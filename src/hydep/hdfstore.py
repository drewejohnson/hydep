"""
============
HDF5 Storage
============

Currently the primary output format.

Format
======

.. note::

    The current version of this file is ``0.1``

----------
Attributes
----------

The following attributes are written as root level attributes

* ``coarseSteps`` ``int`` - Number of coarse time steps

* ``totalSteps`` ``int`` - Number of total times steps, including
  substeps

* ``isotopes`` ``int`` - Number of isotopes in the depletion chain

* ``burnableMaterials`` ``int`` - Number of burnable materials in
  the problem

* ``energyGroups`` ``int`` - Number of energy groups for flux and
  cross sections

* ``fileVersion`` ``int`` ``(2, )`` - Major and minor version of the file

* ``hydepVersion`` ``int`` ``(3, )`` - Version of ``hydep`` package

Datasets
--------

* ``/multiplicationFactor`` ``double`` ``(N_total, 2)`` - Array
  of multiplication factors and absolute uncertainties such that
  ``mf[0, j]`` is the multiplication factor for time point ``j``
  and ``mf[1, j]`` is the associated uncertainty

* ``/fluxes`` ``double`` ``(N_total, N_bumats, N_groups)`` -
  Array of fluxes [n/s] in each burnable material. Note: fluxes are
  normalized to a constant power but are not scaled by volume

* ``/cpuTimes`` ``double`` ``(N_total, )`` - Array of cpu time [s]
  taken at each transport step, both high fidelity and reduced order

* ``/compositions`` ``double`` ``(N_total, N_bumats, N_isotopes)`` -
  Array of atom densities [#/b-cm] for each material at each point
  in time. The density of isotope ``i`` at point ``j`` in material
  ``m`` is ``c[j, m, i]``.

------
Groups
------

``/fissionMatrix`` group
-------------------------

If the fission matrix is present on at least one transport result,
then this group will be created. The following attributes will be
written to this group:

* ``structure`` ``str`` - Sparsity structure of the fission matrices.
  Currently ``"csr"``, indicating a Compressed Sparse Row storage.

All matrices will have shape ``(N_bumats, N_bumats)``, but their
structure may change from step to step.

The fission matrix generated at step ``i`` will be written into a
subgroup.

``/fissionMatrix/<i>`` group
-----------------------------

The subgroup will have the following attributes:

* ``nnz`` ``int`` - Number of non-zero elements in this matrix.

and datasets:

* ``indptr`` ``int`` ``(N_bumats + 1, )`` - Pointer vector
  indicating where non-zero elements for row ``r`` are stored
  in ``data``

* ``indices`` ``int``  (``nnz, )`` - Columns with non-zero
  data such that row ``r`` has non-zero entries in columns
  ``indices[indptr[r]:indptr[r+1]]``

* ``data`` ``double`` ``(nnz, )`` - Vector of non-zero data
  such that non-zero values in row ``r`` are
  ``data[indptr[r]:indptr[r + 1]]``, located corresponding to
  ``indices`` vector.

``/time`` group
---------------

* ``/time/time`` ``double`` ``(N_total, )`` - Vector of points in
  calendar time [s]

* ``/time/highFidelity`` ``bool`` ``(N_total, )`` - Boolean vector
  describing if a specific point corresponds to a high fidelity
  simulation (True) or a reduced order simulation (False)

``/isotopes`` group
-------------------

Group describing isotopes present in the depletion chain, and
their indices in other data sets.

* ``/isotopes/zais`` ``int`` ``(N_isotopes, )`` - Vector
  of isotope ZAI numbers, ordered consistent with the depletion
  chain.
* ``/isotopes/names`` ``S`` ``(N_isotopes,)`` - Vector with isotope
   names, ordered consistent with the depletion chain

``/materials`` group
--------------------

Group describing ordering of burnable materials and their names.
Written to be a consistent ordering across fluxes and compositions

* ``/materials/ids`` ``int`` ``(N_bumats, )`` - Vector with burnable
  material ID
* ``/materials/names`` ``S`` ``(N_bumats, )`` - Vector with material
  names

"""
import numbers
import typing
import pathlib
from collections.abc import Mapping
import bisect
from enum import Enum

import numpy
import h5py
from scipy.sparse import csr_matrix

import hydep
from hydep.constants import SECONDS_PER_DAY
from .store import BaseStore


class HdfEnumKeys(Enum):
    """Enumeration that provides string encoding

    This allows using the instances directly, rather
    than having to make calls to their string values.
    """

    def encode(self, *args, **kwargs):
        """Return a encoded version of the enumeration value

        All arguments are passed directly to :meth:`str.encode`"""
        return self.value.encode(*args, **kwargs)


class HdfStrings(HdfEnumKeys):
    """Strings for root datasets or groups"""

    FLUXES = "fluxes"
    KEFF = "multiplicationFactor"
    ISOTOPES = "isotopes"
    COMPOSITIONS = "compositions"
    CPU_TIMES = "cpuTimes"
    MATERIALS = "materials"
    FISSION_MATRIX = "fissionMatrix"
    CALENDAR = "time"


class HdfSubStrings(HdfEnumKeys):
    """Strings for datasets or groups beyond the base group"""

    MAT_IDS = "ids"
    MAT_NAMES = "names"
    CALENDAR_TIME = "time"
    CALENDAR_HF = "highFidelity"
    ISO_ZAI = "zais"
    ISO_NAMES = "names"


class HdfAttrs(HdfEnumKeys):
    """Strings for populating the attributes dictionary"""

    N_COARSE = "coarseSteps"
    N_TOTAL = "totalSteps"
    N_ISOTOPES = "isotopes"
    N_BMATS = "burnableMaterials"
    N_ENE_GROUPS = "energyGroups"
    V_FORMAT = "fileVersion"
    V_HYDEP = "hydepVersion"


class HdfStore(BaseStore):
    """Write transport and depletion result to HDF files

    Parameters
    ----------
    filename : str, optional
        Name of the file to be written. Default: ``"hydep-results.h5"``
    libver : {"earliest", "latest"}, optional
        Which version of HDF file to write. Passing
        ``"earliest"`` helps with back compatability **with the
        hdf5 library**, while ``"latest"`` may come with
        performance improvements. Default: ``"latest"``
    existOkay : bool, optional
        Raise an error if ``filename`` already exists. Otherwise
        silently overwrite an existing file

    Attributes
    ----------
    VERSION : Tuple[int, int]
        Major and minor version of the stored data. Changes to major
        version will reflect new layouts and/or data has been removed.
        Changes to the minor version reflect new data has been added,
        or performance improvements. Scripts that work for ``x.y`` can
        be expected to also work for ``x.z``, but compatability between
        ``a.b`` and ``c.d`` is not guaranteed.
    fp : pathlib.Path
        Read-only attribute with the absolute path of the intended result
        result file

    Raises
    ------
    OSError
        If ``filename`` exists and is not a file
    FileExistsError
        If ``filename`` exists, is a file, and ``existOkay``
        evaluates to ``False``.

    """

    _VERSION = (0, 1)

    def __init__(
        self,
        filename: typing.Optional[str] = None,
        libver: typing.Optional[str] = None,
        existOkay: typing.Optional[bool] = True,
    ):

        if libver is None:
            libver = "latest"

        if filename is None:
            filename = "hydep-results.h5"

        fp = pathlib.Path(filename).resolve()

        if fp.exists():
            if not fp.is_file():
                raise OSError(f"Result file {fp} exists but is not a file")
            if not existOkay:
                raise FileExistsError(
                    f"Refusing to overwrite result file {fp} since existOkay is True"
                )

        with h5py.File(fp, mode="w", libver=libver) as h5f:
            h5f.attrs[HdfAttrs.V_FORMAT] = self.VERSION
            h5f.attrs[HdfAttrs.V_HYDEP] = tuple(
                int(x) for x in hydep.__version__.split(".")[:3]
            )
        self._fp = fp

    @property
    def fp(self):
        return self._fp

    @property
    def VERSION(self):
        return self._VERSION

    def beforeMain(self, nhf, ntransport, ngroups, isotopes, burnableIndexes):
        """Called before main simulation sequence

        Parameters
        ----------
        isotopes : tuple of hydep.internal.Isotope
            Isotopes used in the depletion chain
        burnableIndexes : iterable of [int, str]
            Burnable material ids and names ordered how they
            are used across the sequence

        """
        with h5py.File(self._fp, "a") as h5f:
            for src, dest in (
                (nhf, HdfAttrs.N_COARSE),
                (ntransport, HdfAttrs.N_TOTAL),
                (len(isotopes), HdfAttrs.N_ISOTOPES),
                (len(burnableIndexes), HdfAttrs.N_BMATS),
                (ngroups, HdfAttrs.N_ENE_GROUPS),
            ):
                h5f.attrs[dest] = src

            tgroup = h5f.create_group(HdfStrings.CALENDAR)
            tgroup.create_dataset(HdfSubStrings.CALENDAR_TIME, (ntransport,))
            tgroup.create_dataset(HdfSubStrings.CALENDAR_HF, (ntransport,), dtype=bool)

            h5f.create_dataset(HdfStrings.KEFF, (ntransport, 2))

            h5f.create_dataset(HdfStrings.CPU_TIMES, (ntransport,))

            h5f.create_dataset(
                HdfStrings.FLUXES, (ntransport, len(burnableIndexes), ngroups)
            )

            h5f.create_dataset(
                HdfStrings.COMPOSITIONS,
                (ntransport, len(burnableIndexes), len(isotopes)),
            )

            isogroup = h5f.create_group(HdfStrings.ISOTOPES)
            zai = numpy.empty(len(isotopes), dtype=int)
            names = numpy.empty_like(zai, dtype=object)

            for ix, iso in enumerate(isotopes):
                zai[ix] = iso.zai
                names[ix] = iso.name

            isogroup[HdfSubStrings.ISO_ZAI] = zai
            isogroup[HdfSubStrings.ISO_NAMES] = names.astype("S")

            materialgroup = h5f.create_group(HdfStrings.MATERIALS)
            mids = materialgroup.create_dataset(
                HdfSubStrings.MAT_IDS, (len(burnableIndexes),), dtype=int
            )
            names = numpy.empty_like(mids, dtype=object)

            for ix, (matid, name) in enumerate(burnableIndexes):
                mids[ix] = matid
                names[ix] = name

            materialgroup[HdfSubStrings.MAT_NAMES] = names.astype("S")

    def postTransport(self, timeStep, transportResult) -> None:
        """Store transport results

        Transport results will come both after high fidelity
        and reduced order solutions.

        Parameters
        ----------
        timeStep : hydep.internal.TimeStep
            Point in calendar time from where these results were
            generated
        transportResult : hydep.internal.TransportResult
            Collection of data. Guaranteed to have at least
            a ``flux`` and ``keff`` attribute that are not
            ``None``

        """
        with h5py.File(self._fp, mode="a") as h5f:
            timeindex = timeStep.total
            tgroup = h5f[HdfStrings.CALENDAR]
            tgroup[HdfSubStrings.CALENDAR_TIME][timeindex] = timeStep.currentTime
            tgroup[HdfSubStrings.CALENDAR_HF][timeindex] = not bool(timeStep.substep)

            h5f[HdfStrings.KEFF][timeindex] = transportResult.keff

            h5f[HdfStrings.FLUXES][timeindex] = transportResult.flux

            cputime = transportResult.runTime
            if cputime is None:
                cputime = numpy.nan
            h5f[HdfStrings.CPU_TIMES][timeindex] = cputime

            fmtx = transportResult.fmtx
            if fmtx is not None:
                fGroup = h5f.get(HdfStrings.FISSION_MATRIX)
                if fGroup is None:
                    fGroup = h5f.create_group(HdfStrings.FISSION_MATRIX)
                    fGroup.attrs["structure"] = "csr"
                    fGroup.attrs["shape"] = fmtx.shape
                thisG = fGroup.create_group(str(timeindex))
                thisG.attrs["nnz"] = fmtx.nnz
                for attr in {"data", "indices", "indptr"}:
                    thisG[attr] = getattr(fmtx, attr)

    def writeCompositions(self, timeStep, compBundle) -> None:
        """Write (potentially) new compositions

        Parameters
        ----------
        timeStep : hydep.internal.TimeStep
            Point in calendar time that corresponds to the
            compositions, e.g. compositions are from this point
            in time
        compBundle : hydep.internal.CompBundle
            New compositions. Will contain ordering of isotopes and
            compositions ordered consistent with the remainder
            of the sequence and corresponding argument to
            :meth:`beforeMain`

        """
        with h5py.File(self._fp, mode="a") as h5f:
            h5f[HdfStrings.COMPOSITIONS][timeStep.total] = compBundle.densities


class HdfProcessor(Mapping):
    """Dictionary-like interface for HDF result files

    Properties like :attr:`zais` are generated at each
    call.

    Parameters
    ----------
    fpOrGroup : str or pathlib.Path or h5py.File or h5py.Group
        Either the name of the result file, an already opened
        HDF file or a group inside an opened HDF file

    Attributes
    ----------
    days : numpy.ndarray
        Points in calendar time for all provided values
    names : tuple of str
        Isotope names ordered consistent with :attr:`zai`.

    """

    _EXPECTS = (0, 1)

    def __init__(
        self, fpOrGroup: typing.Union[str, pathlib.Path, h5py.File, h5py.Group]
    ):
        if isinstance(fpOrGroup, (str, pathlib.Path)):
            self._root = h5py.File(fpOrGroup, mode="r")
        elif isinstance(fpOrGroup, (h5py.File, h5py.Group)):
            self._root = fpOrGroup
        else:
            raise TypeError(f"Type {type(fpOrGroup)} not supported")

        version = self._root.attrs.get("fileVersion")
        if version is None:
            raise KeyError(f"Could not find file version in {self._root}")
        elif tuple(version[:]) != self._EXPECTS:
            raise ValueError(
                f"Found {version[:]} in {self._root}, expected {self._EXPECTS}"
            )

        self.days = numpy.divide(self._root["time/time"], SECONDS_PER_DAY)
        self._names = None

    def __len__(self) -> int:
        return len(self._root)

    def __getitem__(self, key: str) -> typing.Union[h5py.Group, h5py.Dataset]:
        """Fetch a group or dataset directly from the file"""
        return self._root[key]

    def __iter__(self):
        return iter(self._root)

    def __contains__(self, key: str) -> bool:
        """Dictionary-like membership testing of ``key``"""
        return key in self._root

    def get(
        self, key: str, default: typing.Optional = None
    ) -> typing.Optional[typing.Any]:
        """Fetch a group or dataset from the file

        Parameters
        ----------
        key : str
            Name of the dataset or group of interest. Can contain
            multiple levels, e.g. ``"time/time"``
        default : object, optional
            Item to return if ``key`` is not found. Defaults to ``None``

        Returns
        -------
        object
            If ``key`` is found, will be either a :class:`h5py.Group`
            or :class:`h5py.Dataset`. Otherwise ``default`` is returned
        """
        return self._root.get(key, default)

    def keys(self):
        return self._root.keys()

    def values(self):
        return self._root.values()

    def items(self):
        return self._root.items()

    @property
    def zais(self) -> h5py.Dataset:
        """Ordered isotopic ZAI identifiers"""
        return self._root[HdfStrings.ISOTOPES][HdfSubStrings.ISO_ZAI]

    @property
    def names(self) -> typing.Tuple[str, ...]:
        # Stored on the processor to avoid decoding at every call
        if self._names is None:
            ds = self._root[HdfStrings.ISOTOPES][HdfSubStrings.ISO_NAMES]
            self._names = tuple((n.decode() for n in ds))
        return self._names

    @property
    def keff(self) -> h5py.Dataset:
        """Nx2 array with multiplication factor and absolute uncertainty

        Values will be provided for all transport solutions, even reduced
        order simulations that may not compute :math:`k_{eff}`. To obtain
        values at the high-fidelity points, see :meth:`getKeff`
        """
        return self._root[HdfStrings.KEFF]

    @property
    def hfFlags(self) -> h5py.Dataset:
        """Boolean vector indicating high fidelity (True) or reduced order solutions"""
        return self._root[HdfStrings.CALENDAR][HdfSubStrings.CALENDAR_HF][:]

    @property
    def fluxes(self) -> h5py.Dataset:
        """NxMxG array with fluxes in each burnable region

        Will be of shape ``(nTransport, nBurnable, nGroups)``

        """
        return self._root[HdfStrings.FLUXES]

    @property
    def compositions(self) -> h5py.Dataset:
        """NxMxI array with isotopic compositions

        Will be of shape ``(nTransport, nBurnable, nGroups)``

        """
        return self._root[HdfStrings.COMPOSITIONS]

    def getKeff(
        self, hfOnly: typing.Optional[bool] = True
    ) -> typing.Tuple[numpy.ndarray, numpy.ndarray]:
        """Fetch the multiplication factor paired with the time in days

        Parameters
        ----------
        hfOnly : bool, optional
            Return the days and :math:`k` from high-fidelity solutions
            only [default]. Useful if the reduced order code does not
            compute / return :math:`k`.

        Returns
        -------
        days : numpy.ndarray
            Points in time [d] where :math:`k` has been evaluated
        keff : numpy.ndarray
            2D array with multiplication factor in the first column,
            absolute uncertainties in the second column

        """
        slicer = self.hfFlags if hfOnly else slice(None)
        return self.days[slicer], self.keff[slicer, :]

    def getFluxes(
        self, days: typing.Optional[typing.Union[float, typing.Iterable[float]]] = None
    ) -> numpy.ndarray:
        """Retrieve the flux at some or all time points

        Parameters
        ----------
        days : float or iterable of float, optional
            Specific day or days to obtain the flux. If multiple days
            are given, they must be in an increasing order

        Returns
        -------
        numpy.ndarray
            Fluxes at specified days. If ``day`` is a float, shape will
            be ``(nBurnable, nGroups)``. Otherwise, it will be
            ``(nDays, nBurnable, nGroups)`` where ``len(days) == nDays``

        Raises
        ------
        IndexError
            If ``days`` or an element of ``days`` was not found in
            :attr:`days`

        """
        # TODO Add group, material slicing
        if days is None:
            dayslice = slice(None)
        elif isinstance(days, numbers.Real):
            dayslice = bisect.bisect_left(self.days, days)
            if dayslice == len(self.days) or days != self.days[dayslice]:
                raise IndexError(f"Day {days} not found")
        else:
            # Let numpy handle to searching
            reqs = numpy.asarray(days)
            if len(reqs.shape) != 1:
                raise ValueError("Days can only be 1D")
            if (reqs[:-1] - reqs[1:] > 0).any():
                raise ValueError("Days must be in increasing order, for now")
            dayslice = numpy.searchsorted(self.days, reqs)
            for ix, d in zip(dayslice, reqs):
                if ix == len(self.days) or self.days[ix] != d:
                    raise IndexError(f"Day {d} not found")
        return self.fluxes[dayslice]

    def getFissionMatrix(self, day: float) -> csr_matrix:
        """Retrieve the fission matrix for a given day

        Parameters
        ----------
        day : float
            Time in days that the matrix is requested

        Returns
        -------
        scipy.sparse.csr_matrix
            Fission matrix at time ``day``. Rows and columns correspond
            to unique burnable materials

        Raises
        ------
        KeyError
            If the fission matrix group is not defined
        IndexError
            If ``day`` was not found in :attr:`days`

        """
        fmtxGroup = self.get(HdfStrings.FISSION_MATRIX)
        if fmtxGroup is None:
            raise KeyError(
                "fissionMatrix group not found. Likely not included in simulation"
            )
        structure = fmtxGroup.attrs.get("structure")
        if structure != "csr":
            raise ValueError("Expected csr matrix structure, not {structure}")

        ix = bisect.bisect_left(self.days, day)
        if ix == len(self.days) or self.days[ix] != day:
            raise IndexError(f"Day {day} not found")

        group = fmtxGroup[str(ix)]

        return csr_matrix((group["data"], group["indices"], group["indptr"]))
