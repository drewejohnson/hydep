"""
The Serpent solver!
"""

import time
import tempfile
import shutil
import pathlib
import warnings
import zipfile
import logging

import numpy
import hydep
from hydep.internal import configmethod, TransportResult
import hydep.internal.features as hdfeat

from .writer import SerpentWriter
from .runner import SerpentRunner
from .processor import SerpentProcessor, FissionYieldFetcher
from .xsavail import XS_2_1_30


__logger__ = logging.getLogger("hydep.serpent")


class SerpentSolver(hydep.lib.HighFidelitySolver):
    """Primary entry point for using Serpent as high fidelity solver

    Configuration should be done through the :meth:`configure` method.

    Attributes
    ----------
    features : hydep.internal.features.FeatureCollection
        Capabilities employed by this code that are relevant for
        this sequence.
    hooks : hydep.internal.features.FeatureCollection or None
        Hooks describing the physics needed by attached physics
        solvers. Setting this more than once will produced
        warnings, as it should not be modified after use.
    """

    _FEATURES = hdfeat.FeatureCollection(
        (
            hdfeat.FISSION_MATRIX,
            hdfeat.FISSION_YIELDS,
            hdfeat.HOMOG_GLOBAL,
            hdfeat.HOMOG_LOCAL,
            hdfeat.MICRO_REACTION_XS,
        ),
        XS_2_1_30,
    )

    def __init__(self):
        self._hooks = None
        self._curfile = None
        self._tmpdir = None
        self._tmpFile = None
        self._writer = SerpentWriter()
        self._runner = SerpentRunner()
        self._processor = SerpentProcessor()
        self._archiveOnSuccess = False
        self._volumes = None

    @property
    def hooks(self):
        return self._hooks

    @hooks.setter
    def hooks(self, value):
        # TODO Guard against hooks that aren't supported
        if not isinstance(value, hdfeat.FeatureCollection):
            raise TypeError(
                "Hooks must be {}, not {}".format(
                    hdfeat.FeatureCollection.__name__, type(value)
                )
            )
        if self._hooks is not None:
            warnings.warn(f"Overwritting hooks for {self}")

        __logger__.debug(f"Updating hooks: {value}")

        self._hooks = value
        self._writer.hooks = value

    @property
    def features(self):
        return self._FEATURES

    @configmethod
    def configure(self, config):
        """Configure this interface

        Passes configuration data onto the :class:`SerpentWriter`,
        :class:`SerpentRunner`, and :class:`SerpentProcessor` used by
        this solver. Settings are processed according to the following
        sections or keys:

            1. ``"hydep"`` - Global settings
            2. ``"hydep.montecarlo"`` - MC specific settings like particle
               statistics
            3. ``"hydep.serpent"`` - Serpent specific settings like
               executable path and cross section libraries.

        Settings found in later sections will overwrite those found in
        earlier sections.

        Settings that apply directly to the solver:

        * ``archive on success`` [boolean] - Create an archive of
          output files generated by Serpent. Otherwise an archive
          will be created only on failure. This setting can be found
          in any level

        Parameters
        ----------
        config : str or collections.abc.Mapping or pathlib.Path or configparser.ConfigParser
            Configuration options to be processed. If ``str`` or
            ``pathlib.Path``, assume a file and read using
            :meth:`configparser.ConfigParser.read_file`. If a ``dict`` or
            other ``Mapping``, process with
            :meth:`configparser.ConfigParser.read_dict`. Otherwise load
        """

        if config.has_section("hydep"):
            section = config["hydep"]
            self._configure(section)
            self._writer.configure(section, level=0)

        if config.has_section("hydep.montecarlo"):
            section = config["hydep.montecarlo"]
            self._configure(section)
            self._writer.configure(section, level=1)

        if config.has_section("hydep.serpent"):
            section = config["hydep.serpent"]
            self._configure(section)
            self._writer.configure(section, level=2)
            self._runner.configure(section)
            self._processor.configure(section)

    def _configure(self, section):
        self._archiveOnSuccess = section.getboolean(
            "archive on success", fallback=self._archiveOnSuccess
        )

    def bosUpdate(self, compositions, timestep, power):
        """Create a new input file with updated compositions

        Parameters
        ----------
        compositions : hydep.internal.CompBundle
            New compositions for this point in time such that
            ``compositions.densities[i][j]`` is the updated
            atom density for ``compositions.zai[j]`` for material
            ``i``
        timestep : hydep.internal.TimeStep
            Current point in calendar time for the beginning
            of this coarse step
        power : float
            Current reactor power [W]
       """
        self._curfile = self._writer.writeSteadyStateFile(
            f"./serpent/s{timestep.coarse}", compositions, timestep, power
        )

    def eolUpdate(self, compositions, timestep, power):
        """Write the final input file

        Nearly identical to the file generated by :meth:`bosUpdate`, except
        no depletion will be provided.

        Parameters
        ----------
        compositions : hydep.internal.CompBundle
            New compositions for this point in time such that
            ``compositions.densities[i][j]`` is the updated
            atom density for ``compositions.zai[j]`` for material
            ``i``
        timestep : hydep.internal.TimeStep
            Current point in calendar time for the beginning
            of this coarse step
        power : float
            Current power [W]

        """
        self._curfile = self._writer.writeSteadyStateFile(
            f"./serpent/s{timestep.coarse}", compositions, timestep, power, final=True
        )

    def setHooks(self, needs):
        """Instruct the solver and helpers what physics are needed

        Parameters
        ----------
        needs : hydep.internal.features.FeatureCollection
            The needs of other solvers, e.g.
            :class:`hydep.ReducedOrderSolver`

        """
        self.hooks = needs
        self._writer.hooks = needs

    def execute(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._tmpFile = pathlib.Path(self._tmpdir.name) / self._curfile.name
        shutil.move(self._curfile, self._tmpFile)

        start = time.time()
        self._runner(self._tmpFile)

        return time.time() - start

    def processResults(self):
        """Pull necessary information from Serpent outputs

        Returns
        -------
        TransportResult
            At the very least a result containing the flux in each burnable
            region and multiplication factor. Other data will be attached
            depending on the :attr:`hooks`, including fission matrix
            and macrosopic cross sections

        Raises
        ------
        AttributeError
            If the ordering of burnable materials is not known. This
            should be set prior to :meth:`execute`, typically in
            :meth:`bosUpdate` or :meth:`beforeMain`

        """
        base = str(self._tmpFile)

        # TODO Divide fluxes by volumes here VVV
        if self.hooks is not None and self.hooks.macroXS:
            res = self._processor.processResult(base + "_res.m", self.hooks.macroXS)
        else:
            keff = self._processor.getKeff(base + "_res.m")
            fluxes = self._processor.processDetectorFluxes(base + "_det0.m", "flux")
            res = TransportResult(fluxes, keff)

        res.flux = res.flux / self._volumes

        if not self.hooks:
            return res

        for feature in self.hooks.features:
            if feature is hdfeat.FISSION_MATRIX:
                res.fmtx = self._processor.processFmtx(base + "_fmtx0.m")
            elif feature is hdfeat.MICRO_REACTION_XS:
                res.microXS = self._processor.processMicroXS(base + "_mdx0.m")
            elif feature is hdfeat.FISSION_YIELDS:
                res.fissionYields = self._processor.processFissionYields(base + "_det0.m")

        return res

    def finalize(self, status):
        if self._curfile is not None and (self._archiveOnSuccess or not status):
            self._archive()
        self._tmpdir.cleanup()

    def _archive(self):
        skipExts = {".seed", ".out", ".dep"}
        zipf = self._curfile.with_suffix(".zip")

        __logger__.debug(f"Archiving Serpent results to {zipf.resolve()}")

        with zipfile.ZipFile(zipf, "w") as myzip:
            for ff in self._tmpFile.parent.glob("*"):
                for ext in skipExts:
                    if ff.name.endswith(ext):
                        break
                else:
                    myzip.write(ff, ff.name)

    def beforeMain(self, model, orderedBumat, chain):
        """Prepare the base input file

        Parameters
        ----------
        model : hydep.Model
            Geometry information to be written once
        orderedBumat : iterable of hydep.BurnableMaterial
            Burnable materials, ordered to be consistent with
            the framework. Necessary to properly order fluxes and
            other data requested via :attr:`hooks
        chain : hydep.DepletionChain
            Information on isotopes and reactions that may be considered

        """
        matids = []
        self._volumes = numpy.empty((len(orderedBumat), 1))
        for ix, m in enumerate(orderedBumat):
            matids.append(str(m.id))
            self._volumes[ix] = m.volume

        self._writer.model = model
        self._writer.burnable = orderedBumat
        self._writer.updateProblemIsotopes((iso.triplet for iso in chain))

        __logger__.debug("Writing base Serpent input file")

        basefile = pathlib.Path.cwd() / "serpent" / "base.sss"
        self._writer.writeBaseFile(basefile)

        self._processor.burnable = matids

        # Not super pretty, as this interacts both with the writer's roles
        # and the processors roles
        if hdfeat.FISSION_YIELDS in self.hooks.features:
            fyproc = FissionYieldFetcher(matids, chain)
            detlines = fyproc.makeDetectors(upperEnergy=20)
            if detlines:
                with basefile.open("a") as s:
                    s.write("\n".join(detlines))
            self._processor.fyHelper = fyproc
