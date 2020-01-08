from collections import namedtuple
import pathlib

import pytest
import hydep
import hydep.internal
import hydep.serpent
import hydep.internal.features as hdfeat

from tests.regressions import config, ResultComparator


ProblemProxy = namedtuple("ProblemProxy", "model burnable")


@pytest.fixture
def serpentModel(simpleChain, toy2x2lattice):
    # Include the chain so reactions are present
    model = hydep.Model(toy2x2lattice)
    model.differentiateBurnableMaterials(updateVolumes=False)

    burnable = tuple(model.root.findBurnableMaterials())
    for m in burnable:
        m.volume = 1.0

    yield ProblemProxy(model, burnable)


@pytest.fixture
def serpentSolver(tmpdir):
    options = {
        "hydep": {"archive on success": True},
        "hydep.serpent": {
            "random seed": 12345678910,
            "boundary conditions": "reflective",
            "particles": 100,
            "generations per batch": 2,
            "active": 5,
            "skipped": 2,
            "executable": "sss2",
        },
    }

    solver = hydep.serpent.SerpentSolver()
    solver.configure(options)

    with tmpdir.as_cwd():
        tmpdir.mkdir("serpent")
        yield solver
        solver.finalize(True)


@pytest.mark.serpent
def test_serpentSolver(serpentSolver, serpentModel):
    model = serpentModel.model
    burnable = serpentModel.burnable

    # Set hooks for slightly realistic problem
    XS_KEYS = {"abs", "fiss"}
    hooks = hdfeat.FeatureCollection(
        {hdfeat.HOMOG_LOCAL, hdfeat.FISSION_MATRIX}, XS_KEYS
    )

    serpentSolver.setHooks(hooks)

    assert serpentSolver.hooks == hooks

    serpentSolver.beforeMain(model, burnable)

    timeStep = hydep.internal.TimeStep(0, 0, 0, 0)

    # Concentrations for the current step
    # Not sure how these will look in the future
    # Set to None to catch errors down the road

    DUMMY_CONCS = None

    # Set a realistic power for this time step

    POWER = 6e6

    serpentSolver.bosUpdate(DUMMY_CONCS, timeStep, POWER)

    serpentSolver.execute()

    res = serpentSolver.processResults()

    tester = ResultComparator(pathlib.Path(__file__).parent)
    tester.main(res)