"""
Depletion manager

Control time-steps, material divisions, depletion, etc
"""

import numbers
from collections.abc import Sequence
from itertools import repeat

import numpy

from hydep import Model, BurnableMaterial, DepletionChain
from hydep.typed import TypedAttr, IterableOf
from hydep.internal.features import FeatureCollection, MICRO_REACTION_XS


__all__ = ["Manager"]


class Manager:
    """Primary depletion manager

    Responsible for depleting materials and updating compositions

    Parameters
    ----------
    model : hydep.Model
        Geometry and materials
    chain : hydep.DepletionChain
        Chain describing how isotopes decay and transmute
    daysteps : iterable of float
        Length in time [d] for each coarse depletion step
    power : float or iterable of float
        Power [W] to deplete the entire problem. If a single value
        is given, a constant power will be used. Otherwise, must
        have the same number of elements as ``daysteps``, corresponding
        to a constant power in each depletion step
    numPreliminary : int, optional
        Number of coarse depletion steps to take before engaging in
        coupled behavior. Useful for approaching some equilibrium value
        with smaller steps with the high fidelity code

    Attributes
    ----------
    model : hydep.Model
        Geometry and materials
    chain : hydep.DepletionChain
        Depletion chain
    timesteps : numpy.ndarray
        Length of time [s] for each coarse depletion step
    power : numpy.ndarray
        Power [W] to use for each coarse depletion steps
    numPreliminary : int
        Number of coarse depletion steps to take before engaging in
        coupled behavior. Useful for approaching some equilibrium value
        with smaller steps with the high fidelity code
    burnable : None or tuple of hydep.BurnableMaterial
        Ordering of burnable materials. Must be set prior to depletion
        to maintain a consistent mapping from reaction rates and
        compositions
    needs : hydep.internal.features.FeatureCollection
        Read-only property describing the capabilities any solver must
        have in order to properly perform the depletion analysis.
        Currently requires calculation of isotopic reaction cross
        sections in each burnable material, as well as the flux.
    preliminarySteps : Iterator
        Pairs of :attr:`timesteps` and :attr:`power` for the
        preliminary stages.
    activeSteps : Iterator
        Pairs of :attr:`timesteps` and :attr:`power` for the
        active stages after any preliminary steps.
    """

    model = TypedAttr("model", Model)
    chain = TypedAttr("chain", DepletionChain)
    _burnable = IterableOf("burnable", BurnableMaterial, allowNone=True)

    def __init__(self, model, chain, daysteps, power, numPreliminary=0):
        self.model = model
        self.chain = chain
        daysteps = numpy.asarray(daysteps, dtype=float)
        assert (daysteps[:1] - daysteps[:-1] > 0).all()
        self.timesteps = tuple(daysteps * 86400)

        if isinstance(power, numbers.Real):
            assert power > 0
            self.power = tuple(repeat(power, len(self.timesteps)))
        elif isinstance(power, Sequence):
            assert len(power) == len(self.timesteps)
            for p in power:
                assert isinstance(p, numbers.Real)
                assert p > 0
            self.power = tuple(power)

        self._burnable = None
        if numPreliminary is None:
            self._nprelim = 0
        else:
            assert isinstance(numPreliminary, numbers.Integral)
            assert 0 <= numPreliminary < len(self.timesteps)
            self._nprelim = numPreliminary

    @property
    def burnable(self):
        return self._burnable

    @property
    def needs(self):
        return FeatureCollection({MICRO_REACTION_XS})

    @property
    def numPreliminary(self):
        return self._nprelim

    @property
    def preliminarySteps(self):
        return zip(self.timesteps[:self._nprelim], self.power[:self._nprelim])

    @property
    def activeSteps(self):
        return zip(self.timesteps[self._nprelim:], self.power[self._nprelim:])

    def beforeMain(self, model):
        # Count and differentiate burnable materials
        bumatCount = model.countBurnableMaterials()
        for mat, count in bumatCount.values():
            if mat.volume is None:
                raise AttributeError("{} {} does not have volume.".format(
                        mat.__class__.__name__, mat.name))
            mat.volume = mat.volume / count
        model.differentiateBurnableMaterials()
        self._burnable = tuple(model.findBurnableMaterials())
        for ix, mat in enumerate(self._burnable):
            mat.index = ix

    def checkCompatibility(self, hf):
        # Check for compatibility with high fidelity solver
        pass

    def pushResults(self, time, results):
        pass

    def deplete(self, time):
        pass

    def beforeROM(self, time):
        pass