from ..simulation import SimulatorAdapter, SimulationComponent
from ..models import ConnectivitySet
from ..helpers import ListEvalConfiguration
from ..exceptions import *
import os, json, weakref, numpy as np
from itertools import chain
from sklearn.neighbors import KDTree


class MapsScaffoldIdentifiers:
    def reset_identifiers(self):
        self.nest_identifiers = []
        self.scaffold_identifiers = []
        self.scaffold_to_nest_map = {}

    def _build_identifier_map(self):
        self.scaffold_to_nest_map = dict(
            zip(self.scaffold_identifiers, self.nest_identifiers)
        )

    def get_nest_ids(self, ids):
        return [self.scaffold_to_nest_map[id] for id in ids]


class NestCell(SimulationComponent, MapsScaffoldIdentifiers):

    node_name = "simulations.?.cell_models"
    required = ["parameters"]

    def boot(self):
        self.receptor_specifications = {}
        self.reset()
        # The cell model contains a 'parameters' attribute and many sets of
        # neuron model specific sets of parameters. Each set of neuron model
        # specific parameters can define receptor specifications.
        # Extract those if present to the designated receptor_specifications dict.
        for neuron_model in self.__dict__:
            model_parameters = self.__dict__[neuron_model]
            # Exclude the default parameters dict and transfer the receptor specifications
            if (
                neuron_model != "parameters"
                and isinstance(model_parameters, dict)
                and "receptors" in model_parameters
            ):
                self.receptor_specifications[neuron_model] = model_parameters["receptors"]
                del model_parameters["receptors"]

    def validate(self):
        pass

    def reset(self):
        self.reset_identifiers()

    def get_parameters(self):
        # Get the default synapse parameters
        params = self.parameters.copy()
        # Raise an exception if the requested model is not configured.
        if not hasattr(self, self.neuron_model):
            raise Exception(
                "Missing parameters for '{}' model in '{}'".format(
                    self.neuron_model, self.name
                )
            )
        # Merge in the model specific parameters
        params.update(self.__dict__[self.neuron_model])
        return params

    def get_receptor_specifications(self):
        return (
            self.receptor_specifications[self.neuron_model]
            if self.neuron_model in self.receptor_specifications
            else {}
        )


class NestConnection(SimulationComponent):
    node_name = "simulations.?.connection_models"

    casts = {"synapse": dict, "connection": dict}

    required = ["synapse", "connection"]

    defaults = {
        "plastic": False,
        "hetero": None,
        "teaching": None,
        "is_teaching": False,
    }

    def validate(self):
        if "weight" not in self.connection:
            raise ConfigurationException(
                "Missing 'weight' in the connection parameters of "
                + self.node_name
                + "."
                + self.name
            )
        if self.plastic:
            # Set plasticity synapse dict defaults
            synapse_defaults = {
                "A_minus": 0.0,
                "A_plus": 0.0,
                "Wmin": 0.0,
                "Wmax": 4000.0,
            }
            for key, value in synapse_defaults.items():
                if key not in self.synapse:
                    self.synapse[key] = value

    def get_synapse_parameters(self, synapse_model_name):
        # Get the default synapse parameters
        return self.synapse[synapse_model_name]

    def get_connection_parameters(self):
        # Get the default synapse parameters
        params = self.connection.copy()
        # Add the receptor specifications, if required.
        if self.should_specify_receptor_type():
            # If specific receptors are specified, the weight should always be positive.
            params["weight"] = np.abs(params["weight"])
            params["receptor_type"] = self.get_receptor_type()
        params["model"] = self.adapter.suffixed(self.name)
        return params

    def _get_cell_types(self, key="from"):
        meta = self.scaffold.output_formatter.get_connectivity_set_meta(self.name)
        if key + "_cell_types" in meta:
            cell_types = set()
            for name in meta[key + "_cell_types"]:
                cell_types.add(self.scaffold.get_cell_type(name))
            return list(cell_types)
        connection_types = self.scaffold.output_formatter.get_connectivity_set_connection_types(
            self.name
        )
        cell_types = set()
        for connection_type in connection_types:
            cell_types |= set(connection_type.__dict__[key + "_cell_types"])
        return list(cell_types)

    def get_cell_types(self):
        return self._get_cell_types(key="from"), self._get_cell_types(key="to")

    def should_specify_receptor_type(self):
        _, to_cell_types = self.get_cell_types()
        if len(to_cell_types) > 1:
            raise NotImplementedError(
                "Specifying receptor types of connections consisiting of more than 1 cell type is currently undefined behaviour."
            )
        to_cell_type = to_cell_types[0]
        to_cell_model = self.adapter.cell_models[to_cell_type.name]
        return to_cell_model.neuron_model in to_cell_model.receptor_specifications

    def get_receptor_type(self):
        from_cell_types, to_cell_types = self.get_cell_types()
        if len(to_cell_types) > 1:
            raise NotImplementedError(
                "Specifying receptor types of connections consisiting of more than 1 target cell type is currently undefined behaviour."
            )
        if len(from_cell_types) > 1:
            raise NotImplementedError(
                "Specifying receptor types of connections consisting of more than 1 origin cell type is currently undefined behaviour."
            )
        to_cell_type = to_cell_types[0]
        from_cell_type = from_cell_types[0]
        to_cell_model = self.adapter.cell_models[to_cell_type.name]
        if from_cell_type.name in self.adapter.cell_models.keys():
            from_cell_model = self.adapter.cell_models[from_cell_type.name]
        else:  # For neurons receiving from entities
            from_cell_model = self.adapter.entities[from_cell_type.name]
        receptors = to_cell_model.get_receptor_specifications()
        if from_cell_model.name not in receptors:
            raise Exception(
                "Missing receptor specification for cell model '{}' in '{}' while attempting to connect a '{}' to it during '{}'".format(
                    to_cell_model.name, self.node_name, from_cell_model.name, self.name
                )
            )
        return receptors[from_cell_model.name]


class NestDevice(SimulationComponent):
    node_name = "simulations.?.devices"

    casts = {
        "radius": float,
        "origin": [float],
        "parameters": dict,
        "stimulus": ListEvalConfiguration.cast,
    }

    defaults = {"connection_rule": None, "connection_parameters": None}

    required = ["type", "device", "io", "parameters"]

    def validate(self):
        # Fill in the _get_targets method, so that get_target functions
        # according to `type`.
        types = ["local", "cylinder", "cell_type"]
        if self.type not in types:
            raise Exception(
                "Unknown NEST targetting type '{}' in {}".format(
                    self.type, self.node_name
                )
            )
        get_targets_name = "_targets_" + self.type
        method = (
            getattr(self, get_targets_name) if hasattr(self, get_targets_name) else None
        )
        if not callable(method):
            raise Exception(
                "Unimplemented NEST stimulation type '{}' in {}".format(
                    self.type, self.node_name
                )
            )
        self._get_targets = method
        if not self.io == "input" and not self.io == "output":
            raise Exception(
                "Attribute io needs to be either 'input' or 'output' in {}".format(
                    self.node_name
                )
            )
        if hasattr(self, "stimulus"):
            stimulus_name = (
                "stimulus"
                if not hasattr(self.stimulus, "parameter_name")
                else self.stimulus.parameter_name
            )
            self.parameters[stimulus_name] = self.stimulus.eval()

    def get_targets(self):
        """
            Return the targets of the stimulation to pass into the nest.Connect call.
        """
        return self.adapter.get_nest_ids(np.array(self._get_targets(), dtype=int))

    def _targets_local(self):
        """
            Target all or certain cells in a spherical location.
        """
        if len(self.cell_types) != 1:
            # Compile a list of the cells and build a compound tree.
            target_cells = np.empty((0, 5))
            id_map = np.empty((0, 1))
            for t in self.cell_types:
                cells = self.scaffold.get_cells_by_type(t)
                target_cells = np.vstack((target_cells, cells[:, 2:5]))
                id_map = np.vstack((id_map, cells[:, 0]))
            tree = KDTree(target_cells)
            target_positions = target_cells
        else:
            # Retrieve the prebuilt tree from the SHDF file
            tree = self.scaffold.trees.cells.get_tree(self.cell_types[0])
            target_cells = self.scaffold.get_cells_by_type(self.cell_types[0])
            id_map = target_cells[:, 0]
            target_positions = target_cells[:, 2:5]
        # Query the tree for all the targets
        target_ids = tree.query_radius(np.array(self.origin).reshape(1, -1), self.radius)[
            0
        ].tolist()
        return id_map[target_ids]

    def _targets_cylinder(self):
        """
            Target all or certain cells within a cylinder of specified radius.
        """
        if len(self.cell_types) != 1:
            # Compile a list of the cells.
            target_cells = np.empty((0, 5))
            id_map = np.empty((0, 1))
            for t in self.cell_types:
                cells = self.scaffold.get_cells_by_type(t)
                target_cells = np.vstack((target_cells, cells[:, 2:5]))
                id_map = np.vstack((id_map, cells[:, 0]))
            target_positions = target_cells
        else:
            # Retrieve the prebuilt tree from the SHDF file
            # tree = self.scaffold.trees.cells.get_tree(self.cell_types[0])
            target_cells = self.scaffold.get_cells_by_type(self.cell_types[0])
            # id_map = target_cells[:, 0]
            target_positions = target_cells[:, 2:5]
            # Query the tree for all the targets
            center_scaffold = [
                self.scaffold.configuration.X / 2,
                self.scaffold.configuration.Z / 2,
            ]

            # Find cells falling into the cylinder volume
            target_cells_idx = np.sum(
                (target_positions[:, [0, 2]] - np.array(center_scaffold)) ** 2, axis=1
            ).__lt__(self.radius ** 2)
            cylinder_target_cells = target_cells[target_cells_idx, 0]
            cylinder_target_cells = cylinder_target_cells.astype(int)
            cylinder_target_cells = cylinder_target_cells.tolist()
            # print(id_stim)
            return cylinder_target_cells

    def _targets_cell_type(self):
        """
            Target all cells of certain cell types
        """
        cell_types = [self.scaffold.get_cell_type(t) for t in self.cell_types]
        if len(cell_types) != 1:
            # Compile a list of the different cell type cells.
            target_cells = np.empty((0, 1))
            for t in cell_types:
                if t.entity:
                    ids = self.scaffold.get_entities_by_type(t.name)
                else:
                    ids = self.scaffold.get_cells_by_type(t.name)[:, 0]
                target_cells = np.vstack((target_cells, ids))
            return target_cells
        else:
            # Retrieve a single list
            t = cell_types[0]
            if t.entity:
                ids = self.scaffold.get_entities_by_type(t.name)
            else:
                ids = self.scaffold.get_cells_by_type(t.name)[:, 0]
            return ids


class NestEntity(NestDevice, MapsScaffoldIdentifiers):
    node_name = "simulations.?.entities"

    def boot(self):
        super().boot()
        self.reset_identifiers()


class NestAdapter(SimulatorAdapter):
    """
        Interface between the scaffold model and the NEST simulator.
    """

    simulator_name = "nest"

    configuration_classes = {
        "cell_models": NestCell,
        "connection_models": NestConnection,
        "devices": NestDevice,
        "entities": NestEntity,
    }

    casts = {"threads": int, "virtual_processes": int, "modules": list}

    defaults = {
        "default_synapse_model": "static_synapse",
        "default_neuron_model": "iaf_cond_alpha",
        "verbosity": "M_ERROR",
        "threads": 1,
        "resolution": 1.0,
        "modules": [],
    }

    required = [
        "default_neuron_model",
        "default_synapse_model",
        "duration",
        "resolution",
        "threads",
    ]

    def __init__(self):
        super().__init__()
        self.is_prepared = False
        self.suffix = ""
        self.multi = False
        self.has_lock = False
        self.global_identifier_map = {}

        def finalize_self(weak_obj):
            if weak_obj() is not None:
                weak_obj().__safedel__()

        r = weakref.ref(self)
        weakref.finalize(self, finalize_self, r)

    def __safedel__(self):
        if self.has_lock:
            self.release_lock()

    def prepare(self, hdf5):
        if self.is_prepared:
            raise AdapterException(
                "Attempting to prepare the same adapter twice. Please use `scaffold.create_adapter` for multiple adapter instances of the same simulation."
            )
        self.scaffold.report("Importing  NEST...", 2)
        import nest

        self.nest = nest
        self.scaffold.report("Locking NEST kernel...", 2)
        self.lock()
        self.scaffold.report("Installing  NEST modules...", 2)
        self.install_modules()
        if self.in_full_control():
            self.scaffold.report("Initializing NEST kernel...", 2)
            self.reset_kernel()
        self.scaffold.report("Creating neurons...", 2)
        self.create_neurons()
        self.scaffold.report("Creating entities...", 2)
        self.create_entities()
        self.scaffold.report("Building identifier map...", 2)
        self._build_identifier_map()
        self.scaffold.report("Creating devices...", 2)
        self.create_devices()
        self.scaffold.report("Creating connections...", 2)
        self.connect_neurons(hdf5)
        self.is_prepared = True
        return nest

    def in_full_control(self):
        if not self.has_lock or not self.read_lock():
            raise AdapterException(
                "Can't check if we're in full control of the kernel: we have no lock on the kernel."
            )
        return not self.multi or len(self.read_lock()["suffixes"]) == 1

    def lock(self):
        if not self.multi:
            self.single_lock()
        else:
            self.multi_lock()
        self.has_lock = True

    def single_lock(self):
        try:
            lock_data = {"multi": False}
            self.write_lock(lock_data, mode="x")
        except FileExistsError as e:
            raise KernelLockedException(
                "This adapter is not in multi-instance mode and another adapter is already managing the kernel."
            ) from None

    def multi_lock(self):
        lock_data = self.read_lock()
        if lock_data is None:
            lock_data = {"multi": True, "suffixes": []}
        if not lock_data["multi"]:
            raise KernelLockedException(
                "The kernel is locked by a single-instance adapter and cannot be managed by multiple instances."
            )
        if self.suffix in lock_data["suffixes"]:
            raise SuffixTakenException(
                "The kernel is already locked by an instance with the same suffix."
            )
        lock_data["suffixes"].append(self.suffix)
        self.write_lock(lock_data)

    def read_lock(self):
        try:
            with open(self.get_lock_path(), "r") as lock:
                return json.loads(lock.read())
        except FileNotFoundError as e:
            return None

    def write_lock(self, lock_data, mode="w"):
        with open(self.get_lock_path(), mode) as lock:
            lock.write(json.dumps(lock_data))

    def enable_multi(self, suffix):
        self.suffix = suffix
        self.multi = True

    def release_lock(self):
        if not self.has_lock:
            raise AdapterException(
                "Cannot unlock kernel from an adapter that has no lock on it."
            )
        self.has_lock = False
        lock_data = self.read_lock()
        if lock_data["multi"]:
            if len(lock_data["suffixes"]) == 1:
                self.delete_lock_file()
            else:
                lock_data["suffixes"].remove(self.suffix)
                self.write_lock(lock_data)
        else:
            self.delete_lock_file()

    def delete_lock_file(self):
        os.remove(self.get_lock_path())

    def get_lock_name(self):
        return "kernel_" + str(os.getpid()) + ".lck"

    def get_lock_path(self):
        return self.nest.__path__[0] + "/" + self.get_lock_name()

    def reset_kernel(self):
        self.nest.set_verbosity(self.verbosity)
        self.nest.ResetKernel()
        self.set_threads(self.threads)
        self.nest.SetKernelStatus(
            {
                "resolution": self.resolution,
                "overwrite_files": True,
                "data_path": self.scaffold.output_formatter.get_simulator_output_path(
                    self.simulator_name
                ),
            }
        )

    def reset(self):
        self.is_prepared = False
        if hasattr(self, "nest"):
            self.reset_kernel()
        self.global_identifier_map = {}
        for cell_model in self.cell_models.values():
            cell_model.reset()

    def get_master_seed(self):
        # Use a constant reproducible master seed
        return 1989

    def set_threads(self, threads, virtual=None):
        master_seed = self.get_master_seed()
        # Update the internal reference to the amount of threads
        if virtual is None:
            virtual = threads
        # Create a range of random seeds and generators.
        random_generator_seeds = range(master_seed, master_seed + virtual)
        # Create a different range of random seeds for the kernel.
        thread_seeds = range(master_seed + virtual + 1, master_seed + 1 + 2 * virtual)
        success = True
        try:
            # Update the kernel with the new RNG and thread state.
            self.nest.SetKernelStatus(
                {
                    "grng_seed": master_seed + virtual,
                    "rng_seeds": thread_seeds,
                    "local_num_threads": threads,
                    "total_num_virtual_procs": virtual,
                }
            )
        except Exception as e:
            if (
                hasattr(e, "errorname")
                and e.errorname[0:27] == "The resolution has been set"
            ):
                # Threads can't be updated at this point in time.
                success = False
                raise NestKernelException(
                    "Updating the NEST threads or virtual processes must occur before setting the resolution."
                ) from None
            else:
                raise
        if success:
            self.threads = threads
            self.virtual_processes = virtual
            self.random_generators = [
                np.random.RandomState(seed) for seed in random_generator_seeds
            ]

    def simulate(self, simulator):
        if not self.is_prepared:
            self.scaffold.warn("Adapter has not been prepared", SimulationWarning)
        self.scaffold.report("Simulating...", 2)
        simulator.Simulate(self.duration)
        self.scaffold.report("Simulation finished.", 2)
        if self.has_lock:
            self.release_lock()

    def validate(self):
        for cell_model in self.cell_models.values():
            cell_model.neuron_model = (
                cell_model.neuron_model
                if hasattr(cell_model, "neuron_model")
                else self.default_neuron_model
            )
        for connection_model in self.connection_models.values():
            connection_model.synapse_model = (
                connection_model.synapse_model
                if hasattr(connection_model, "synapse_model")
                else self.default_synapse_model
            )
            connection_model.plastic = (
                connection_model.plastic
                if hasattr(connection_model, "plastic")
                else connection_model.defaults["plastic"]
            )
            connection_model.hetero = (
                connection_model.hetero
                if hasattr(connection_model, "hetero")
                else connection_model.defaults["hetero"]
            )
            if connection_model.plastic and connection_model.hetero:
                if not hasattr(connection_model, "teaching"):
                    raise ConfigurationException(
                        "Required attribute 'teaching' is missing for heteroplastic connection '{}'".format(
                            connection_model.get_config_node()
                        )
                    )
                if connection_model.teaching not in self.connection_models:
                    raise ConfigurationException(
                        "Teaching connection '{}' does not exist".format(
                            connection_model.teaching
                        )
                    )
                # Set the is_teaching parameter of teaching connection to true
                teaching_connection = self.connection_models[connection_model.teaching]
                teaching_connection.is_teaching = True
                teaching_connection.add_after(connection_model.name)

    def install_modules(self):
        for module in self.modules:
            print(module)
            try:
                self.nest.Install(module)
            except Exception as e:
                if e.errorname == "DynamicModuleManagementError":
                    if "loaded already" in e.message:
                        self.scaffold.warn(
                            "Module {} already installed".format(module), KernelWarning
                        )
                    elif "file not found" in e.message:
                        raise NestModuleException(
                            "Module {} not found".format(module)
                        ) from None
                else:
                    raise

    def _build_identifier_map(self):
        # Iterate over all simulation components that contain representations
        # of scaffold components with an ID to create a map of all scaffold ID's
        # to all NEST ID's this adapter manages
        for mapping_type in chain(self.entities.values(), self.cell_models.values()):
            # "Freeze" the type's identifiers into a map
            mapping_type._build_identifier_map()
            # Add the type's map to the global map
            self.global_identifier_map.update(mapping_type.scaffold_to_nest_map)

    def get_nest_ids(self, ids):
        return [self.global_identifier_map[id] for id in ids]

    def create_neurons(self):
        """
            Recreate the scaffold neurons in the same order as they were placed,
            inside of the NEST simulator based on the cell model configuration.
        """
        track_models = (
            []
        )  # Keeps track of already added models if there's more than 1 stitch per model
        # Iterate over all the placement stitches: each stitch was a batch of cells placed together and
        # if we don't follow the same order as during the placement, the cell IDs can not be easily matched
        for cell_type_id, start_id, count in self.scaffold.placement_stitching:
            # Get the cell_type name from the type id to type name map.
            name = self.scaffold.configuration.cell_type_map[cell_type_id]
            cell_model = self.cell_models[name]
            nest_name = self.suffixed(name)
            if (
                name not in track_models
            ):  # Is this the first time encountering this model?
                # Create the cell model in the simulator
                self.scaffold.report("Creating " + nest_name + "...", 3)
                self.create_model(cell_model)
                track_models.append(name)
            # Create the same amount of cells that were placed in this stitch.
            self.scaffold.report("Creating {} {}...".format(count, nest_name), 3)
            identifiers = self.nest.Create(nest_name, count)
            cell_model.scaffold_identifiers.extend([start_id + i for i in range(count)])
            cell_model.nest_identifiers.extend(identifiers)

    def create_entities(self):
        # Create entities
        for entity_type in self.entities.values():
            name = entity_type.name
            nest_name = self.suffixed(name)
            count = self.scaffold.statistics.cells_placed[entity_type.name]
            # Create the cell model in the simulator
            self.scaffold.report("Creating " + nest_name + "...", 3)
            entity_nodes = list(self.nest.Create(entity_type.device, count))
            self.scaffold.report("Creating {} {}...".format(count, nest_name), 3)
            if hasattr(entity_type, "parameters"):
                # Execute SetStatus and catch DictError
                self.execute_command(
                    self.nest.SetStatus,
                    entity_nodes,
                    entity_type.parameters,
                    exceptions={
                        "DictError": {
                            "from": None,
                            "exception": catch_dict_error(
                                "Could not create {} device '{}': ".format(
                                    entity_type.device, entity_type.name
                                )
                            ),
                        }
                    },
                )
            entity_type.scaffold_identifiers = self.scaffold.get_entities_by_type(
                entity_type.name
            )
            entity_type.nest_identifiers = entity_nodes

    def connect_neurons(self, hdf5):
        """
            Connect the cells in NEST according to the connection model configurations
        """
        order = NestConnection.resolve_order(self.connection_models)
        for connection_model in order:
            name = connection_model.name
            nest_name = self.suffixed(name)
            dataset_name = "cells/connections/" + name
            if dataset_name not in hdf5:
                self.scaffold.warn(
                    'Expected connection dataset "{}" not found. Skipping it.'.format(
                        dataset_name
                    ),
                    ConnectivityWarning,
                )
                continue
            connectivity_matrix = hdf5[dataset_name]
            # Get the NEST identifiers for the connections made in the connectivity matrix
            presynaptic_cells = self.get_nest_ids(
                np.array(connectivity_matrix[:, 0], dtype=int)
            )
            postsynaptic_cells = self.get_nest_ids(
                np.array(connectivity_matrix[:, 1], dtype=int)
            )
            # Accessing the postsynaptic type to be associated to the volume transmitter of the synapse
            cs = ConnectivitySet(self.scaffold.output_formatter, name)
            postsynaptic_type = cs.connection_types[0].to_cell_types[0]

            # Create the synapse model in the simulator
            self.create_synapse_model(connection_model)
            # Set the specifications NEST allows like: 'rule', 'autapses', 'multapses'
            connection_specifications = {"rule": "one_to_one"}
            # Get the connection parameters from the configuration
            connection_parameters = connection_model.get_connection_parameters()
            # Create the connections in NEST
            self.scaffold.report("Creating connections " + nest_name, 3)
            self.execute_command(
                self.nest.Connect,
                presynaptic_cells,
                postsynaptic_cells,
                connection_specifications,
                connection_parameters,
                exceptions={
                    "IncompatibleReceptorType": {
                        "from": None,
                        "exception": catch_receptor_error(
                            "Invalid receptor specifications in {}: ".format(name)
                        ),
                    }
                },
            )

            # Workaround for https://github.com/alberto-antonietti/CerebNEST/issues/10
            if connection_model.plastic and connection_model.hetero:
                # Create the volume transmitter if the connection is plastic with heterosynaptic plasticity
                self.scaffold.report("Creating volume transmitter for " + name, 3)
                volume_transmitters = self.create_volume_transmitter(
                    connection_model, postsynaptic_cells
                )
                postsynaptic_type._vt_id = volume_transmitters
                # # Associate the volume transmitters to their ids
                # for i,vti in enumerate(volume_transmitters):
                #     self.nest.SetStatus([vti],{"vt_num" : float(i)})

            if connection_model.is_teaching:
                # We need to connect the pre-synaptic neurons also to the volume transmitter associated to each post-synaptic create_neurons
                # suppose that the vt ids are stored in a variable self.cell_models["vt_"+name].identifiers
                postsynaptic_volume_transmitters = [
                    pc - postsynaptic_cells[0] + postsynaptic_type._vt_id[0]
                    for pc in postsynaptic_cells
                ]
                self.nest.Connect(
                    presynaptic_cells,
                    postsynaptic_volume_transmitters,
                    connection_specifications,
                    {"model": "static_synapse", "weight": 1.0, "delay": 1.0},
                )

    def create_devices(self):
        """
            Create the configured NEST devices in the simulator
        """
        for device_model in self.devices.values():
            device = self.nest.Create(device_model.device)
            self.scaffold.report("Creating device:  " + device_model.device, 3)
            # Execute SetStatus and catch DictError
            self.execute_command(
                self.nest.SetStatus,
                device,
                device_model.parameters,
                exceptions={
                    "DictError": {
                        "from": None,
                        "exception": catch_dict_error(
                            "Could not create {} device '{}': ".format(
                                device_model.device, device_model.name
                            )
                        ),
                    }
                },
            )
            device_targets = device_model.get_targets()
            self.scaffold.report(
                "Connecting to {} device targets.".format(len(device_targets)), 3
            )

            try:
                if device_model.io == "input":
                    self.nest.Connect(
                        device,
                        device_targets,
                        {"rule": "all_to_all"},
                        device_model.connection_parameters,
                    )
                elif device_model.io == "output":
                    self.nest.Connect(
                        device_targets,
                        device,
                        {"rule": "all_to_all"},
                        device_model.connection_parameters,
                    )
                else:
                    pass  # Weight recorder device is not connected to any node; just linked to a connection
            except Exception as e:
                if e.errorname == "IllegalConnection":
                    raise Exception(
                        "IllegalConnection error for '{}'".format(
                            device_model.get_config_node()
                        )
                    ) from None
                else:
                    raise

    def create_model(self, cell_model):
        """
            Create a NEST cell model in the simulator based on a cell model configuration.
        """
        # Use the default model unless another one is specified in the configuration.A_minus
        # Alias the nest model name under our cell model name.
        nest_name = self.suffixed(cell_model.name)
        self.nest.CopyModel(cell_model.neuron_model, nest_name)
        # Get the synapse parameters
        params = cell_model.get_parameters()
        # Set the parameters in NEST
        self.nest.SetDefaults(nest_name, params)

    def create_synapse_model(self, connection_model):
        """
            Create a NEST synapse model in the simulator based on a synapse model configuration.
        """
        nest_name = self.suffixed(connection_model.name)
        # Use the default model unless another one is specified in the configuration.
        # Alias the nest model name under our cell model name.
        self.scaffold.report(
            "Copying synapse model '{}' to {}".format(
                connection_model.synapse_model, nest_name
            ),
            3,
        )
        self.nest.CopyModel(connection_model.synapse_model, nest_name)
        # Get the synapse parameters
        params = connection_model.get_synapse_parameters(connection_model.synapse_model)
        # Set the parameters in NEST
        self.nest.SetDefaults(nest_name, params)

    # This function should be simplified by providing a CreateTeacher function in the
    # CerebNEST module. See https://github.com/nest/nest-simulator/issues/1317
    # And https://github.com/alberto-antonietti/CerebNEST/issues/10
    def create_volume_transmitter(self, synapse_model, postsynaptic_cells):
        vt = self.nest.Create("volume_transmitter_alberto", len(postsynaptic_cells))
        teacher = vt[0]
        # Assign the volume transmitters to their synapse model
        self.nest.SetDefaults(synapse_model.name, {"vt": teacher})
        # Assign an ID to each volume transmitter
        for n, vti in enumerate(vt):
            self.nest.SetStatus([vti], {"deliver_interval": 2})  # TO CHECK
            # Waiting for Albe to clarify necessity of this parameter
            self.nest.SetStatus([vti], {"vt_num": n})
        return vt

    def execute_command(self, command, *args, exceptions={}):
        try:
            command(*args)
        except Exception as e:
            if not hasattr(e, "errorname"):
                raise
            if e.errorname in exceptions:
                handler = exceptions[e.errorname]
                if "from" in handler:
                    raise handler["exception"](e) from handler["from"]
                else:
                    raise handler["exception"]
            else:
                raise

    def suffixed(self, str):
        if self.suffix == "":
            return str
        return str + "_" + self.suffix


def catch_dict_error(message):
    def handler(e):
        attributes = list(
            map(lambda x: x.strip(), e.errormessage.split(":")[-1].split(","))
        )
        return NestModelException(
            message + "Unknown attributes {}".format("'" + "', '".join(attributes) + "'")
        )

    return handler


def catch_receptor_error(message):
    def handler(e):
        return NestModelException(message + e.errormessage.split(":")[-1].strip())

    return handler
