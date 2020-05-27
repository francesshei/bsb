import unittest, os, sys, numpy as np, h5py, importlib

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from scaffold.core import Scaffold
from scaffold.config import JSONConfig
from scaffold.models import Layer, CellType, ConnectivitySet
from scaffold.output import MorphologyRepository
from test_setup import get_test_network


def relative_to_tests_folder(path):
    return os.path.join(os.path.dirname(__file__), path)


fiber_transform_config = relative_to_tests_folder("configs/test_fiber_intersection.json")
morpho_file = relative_to_tests_folder("morphologies_fiber.hdf5")


class TestFiberIntersection(unittest.TestCase):
    @classmethod
    def setUpClass(self):
        super(TestFiberIntersection, self).setUpClass()
        # The scaffold has only the Granular layer (100x100x150) with 20 GrCs and 1 GoC placed, as specified in the
        # config file
        config = JSONConfig(file=fiber_transform_config)
        quivers_field = np.zeros(
            shape=(3, 4, 4, 6)
        )  # Each voxel in the volume has vol_res=25um
        basic_quiver = np.array([0, 0.7, 0.7])
        quivers_field[0, :] = basic_quiver[0]
        quivers_field[1, :] = basic_quiver[1]
        quivers_field[2, :] = basic_quiver[2]
        config.connection_types[
            "parallel_fiber_to_golgi_bended"
        ].transformation.quivers = quivers_field
        self.scaffold = Scaffold(config)
        self.scaffold.morphology_repository = MorphologyRepository(morpho_file)
        self.scaffold.compile_network()

    def test_fiber_connections(self):
        pre_type = "granule_cell"
        pre_neu = self.scaffold.get_placement_set(pre_type)
        conn_type = "parallel_fiber_to_golgi"
        cs = self.scaffold.get_connectivity_set(conn_type)
        num_conn = len(cs.connections)
        # Check that no more connections are formed than the number of presynaptic neurons - how could happen otherwise?
        self.assertTrue(num_conn <= len(pre_neu.identifiers))

        # Check that increasing resolution in FiberIntersection does not change connection
        # number if there are no transformations (and thus the fibers are parallel to main axes)
        conn_type_HR = "parallel_fiber_to_golgi_HR"
        cs_HR = self.scaffold.get_connectivity_set(conn_type_HR)
        self.assertEqual(num_conn, len(cs_HR.connections))

        # Check that half (+- 5) connections are obtained with half the affinity
        conn_type_affinity = "parallel_fiber_to_golgi_affinity"
        cs_affinity = self.scaffold.get_connectivity_set(conn_type_affinity)
        self.assertAlmostEqual(num_conn / 2, len(cs_affinity.connections), delta=5)

        # Check that more connections are obtained when the PC surface is oriented according to orientation vector of 45° rotation in yz plane
        conn_type_transform = "parallel_fiber_to_golgi_bended"
        cs_transform = self.scaffold.get_connectivity_set(conn_type_transform)
        self.assertTrue(len(cs_transform.connections) >= num_conn)
