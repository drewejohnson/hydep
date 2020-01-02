"""
The Serpent solver!
"""

import time
import tempfile
import shutil
import pathlib
import warnings

import hydep
from hydep.internal import configmethod
import hydep.internal.features as hdfeat

from .writer import SerpentWriter
from .runner import SerpentRunner
from .processor import SerpentProcessor


class SerpentSolver(hydep.HighFidelitySolver):
    """Primary entry point for using Serpent as high fidelity solver

    Configuration should be done through the :meth:`configure` method.

    Attributes
    ----------
    features : hydep.internal.features.FeatureCollection
        Capabilities employed by this code that are relevant for
        this sequence. Can get basically all the macro XS possible,
        but also not sure how this part of the interface will
        go.
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

    @property
    def hooks(self):
        return self._hooks

    @hooks.setter
    def hooks(self, value):
        if not isinstance(value, hdfeat.FeatureCollection):
            raise TypeError("Hooks must be {}, not {}".format(
                hdfeat.FeatureCollection.__name__, type(value)))
        if self._hooks is not None:
            warnings.warn("Overwritting hooks for {}".format(self))
        self._hooks = value
        self._writer.hooks = value

    @property
    def features(self):
        return hdfeat.FeatureCollection((
            hydep.features.FISSION_MATRIX,
            hydep.features.HOMOG_GLOBAL,
            hydep.features.HOMOG_LOCAL,
            hydep.features.MICRO_REACTION_XS,
        ), (True))

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
            self._writer.configure(config["hydep"], level=0)

        if config.has_section("hydep.montecarlo"):
            self._writer.configure(config["hydep.montecarlo"], level=1)

        if config.has_section("hydep.serpent"):
            section = config["hydep.serpent"]
            self._writer.configure(section, level=2)
            self._runner.configure(section)
            self._processor.configure(section)

    def bosUpdate(self, _compositions, timestep):
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
       """
        self._curfile = self._writer.writeSteadyStateFile(
            "./serpent/s{}".format(timestep.coarse), timestep)

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
        xsnames = set() if self.hooks is None else self.hooks.macroXS
        base = str(self._tmpFile)
        res = self._processor.processResult(base + "_res.m", xsnames)
        if self.hooks is None:
            return res

        if hdfeat.FISSION_MATRIX in self.hooks:
            res.fmtx = self._processor.processFmtx(base + "_fmtx0.m")

        return res

    def finalize(self, _status):
        # TODO Zip on failure
        self._tmpdir.cleanup()

    def beforeMain(self, model, orderedBumat):
        self._writer.model = model
        self._writer.burnable = orderedBumat
        self._writer.writeBaseFile("./serpent/base.sss")
        self._processor.burnable = tuple(str(m.id) for m in orderedBumat)

