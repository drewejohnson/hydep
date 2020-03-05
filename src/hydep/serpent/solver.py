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
from hydep.internal import TransportResult
import hydep.internal.features as hdfeat

from .writer import SerpentWriter, ExtDepWriter
from .runner import SerpentRunner, ExtDepRunner
from .processor import SerpentProcessor, FissionYieldFetcher
from .xsavail import XS_2_1_30


__logger__ = logging.getLogger("hydep.serpent")

_FEATURES_ATLEAST_2_1_30 = hdfeat.FeatureCollection(
    (
        hdfeat.FISSION_MATRIX,
        hdfeat.FISSION_YIELDS,
        hdfeat.HOMOG_GLOBAL,
        hdfeat.HOMOG_LOCAL,
        hdfeat.MICRO_REACTION_XS,
    ),
    XS_2_1_30,
)


class SerpentSolver(hydep.lib.HighFidelitySolver):
    """Primary entry point for using Serpent as high fidelity solver

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
        return _FEATURES_ATLEAST_2_1_30

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
                res.fissionYields = self._processor.processFissionYields(
                    base + "_det0.m"
                )

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

    def beforeMain(self, model, manager, settings):
        """Prepare the base input file

        Parameters
        ----------
        model : hydep.Model
            Geometry information to be written once
        manager : hydep.Manager
            Depletion information
        settings : hydep.settings.HydepSettings
            Shared settings

        """
        self._runner.configure(settings.serpent)

        assert manager.burnable is not None
        orderedBumat = manager.burnable

        matids = []
        self._volumes = numpy.empty((len(orderedBumat), 1))
        for ix, m in enumerate(orderedBumat):
            matids.append(str(m.id))
            self._volumes[ix] = m.volume

        self._writer.model = model
        self._writer.burnable = orderedBumat

        acelib = settings.serpent.acelib
        if acelib is None:
            raise AttributeError(
                f"Serpent acelib setting not configured on {settings}"
            )
        self._writer.updateProblemIsotopes((iso.triplet for iso in manager.chain), acelib)

        __logger__.debug("Writing base Serpent input file")

        basefile = pathlib.Path.cwd() / "serpent" / "base.sss"
        self._writer.writeBaseFile(basefile, settings)

        self._processor.burnable = matids

        # Not super pretty, as this interacts both with the writer's roles
        # and the processors roles
        if hdfeat.FISSION_YIELDS in self.hooks.features:
            fyproc = FissionYieldFetcher(matids, manager.chain)
            detlines = fyproc.makeDetectors(upperEnergy=20)
            if detlines:
                with basefile.open("a") as s:
                    s.write("\n".join(detlines))
            self._processor.fyHelper = fyproc


class CoupledSerpentSolver(hydep.lib.HighFidelitySolver):
    """Utilize the external depletion interface"""

    def __init__(self):
        self._archiveOnSuccess = False
        self._writer = ExtDepWriter()
        self._processor = SerpentProcessor()
        self._runner = ExtDepRunner()
        self._volumes = None
        self._cstep = 0
        self._fp = None

    @property
    def features(self):
        return _FEATURES_ATLEAST_2_1_30

    def beforeMain(self, model, manager, settings):
        # TODO Share some of this with main SerpentSolver
        # TODO Pass some of this init stuff onto BaseWriter
        assert manager.burnable is not None

        self._runner.configure(settings.serpent)

        self._writer.model = model
        self._writer.burnable = manager.burnable

        acelib = settings.serpent.acelib
        if acelib is None:
            raise AttributeError(
                f"Serpent acelib setting not configured on {settings}"
            )
        self._writer.updateProblemIsotopes((iso.triplet for iso in manager.chain), acelib)

        self._volumes = numpy.empty((len(manager.burnable), 1))
        matids = numpy.empty(len(manager.burnable), dtype=object)

        for ix, m in enumerate(manager.burnable):
            self._volumes[ix] = m.volume
            matids[ix] = str(m.id)

        self._processor.burnable = matids.astype(str)

        self._fp = basefile = pathlib.Path.cwd() / "serpent-extdep" / "external"
        self._writer.writeCouplingFile(basefile, manager.timesteps, manager.powers, settings)

        if hdfeat.FISSION_YIELDS in self.hooks.features:
            fyproc = FissionYieldFetcher(matids, manager.chain)
            detlines = fyproc.makeDetectors(upperEnergy=20)
            if detlines:
                with basefile.open("a") as s:
                    s.write("\n".join(detlines))
            self._processor.fyHelper = fyproc

    def bosUpdate(self, compositions, timestep, _power):
        # Skip updating for step 0 as BOL compositions aready written
        if timestep.coarse == 0:
            return
        self._cstep = timestep.coarse
        self._writer.updateComps(compositions, timestep, threshold=0)

    def eolUpdate(self, compositions, timestep, _power):
        self._cstep = -timestep.coarse
        self._writer.updateComps(compositions, timestep, threshold=0)

    def execute(self):
        if self._cstep > 0:
            self._runner.solveNext()
        elif self._cstep == 0:
            self._runner.start(self._fp, self._fp.with_suffix(".log"))
            self._writer.updateFromRestart()
        else:
            self._runner.solveEOL()

    # TODO Share this with SerpentSolver
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

    # TODO Share this with SerpentSolver
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
            If the ordering of burnable materials is not known.

        """
        step = self._cstep if self._cstep >= 0 else -self._cstep
        base = str(self._fp)

        if self.hooks is not None and self.hooks.macroXS:
            res = self._processor.processResult(base + "_res.m", self.hooks.macroXS, step)
        else:
            keff = self._processor.getKeff(base + "_res.m", step)
            fluxes = self._processor.processDetectorFluxes(base + f"_det{step}.m", "flux")
            res = TransportResult(fluxes, keff)

        res.flux = res.flux / self._volumes

        if not self.hooks:
            return res

        for feature in self.hooks.features:
            if feature is hdfeat.FISSION_MATRIX:
                res.fmtx = self._processor.processFmtx(base + f"_fmtx{step}.m")
            elif feature is hdfeat.MICRO_REACTION_XS:
                res.microXS = self._processor.processMicroXS(base + f"_mdx{step}.m")
            elif feature is hdfeat.FISSION_YIELDS:
                res.fissionYields = self._processor.processFissionYields(
                    base + f"_det{step}.m"
                )

        return res
