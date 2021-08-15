
# Standard library includes
from collections import namedtuple
import pickle
import multiprocessing
multiprocessing.set_start_method("spawn")

# Internal project includes
from .. import zeros, MPCCompatibilityError
from ..utils import simulate
from ..evaluation import HoldoutModelEvaluator
from .model_tuner import ModelTuner
from ..sysid import MLPFactory, SINDyFactory, ApproximateGPModelFactory, ARXFactory, KoopmanFactory

# External library includes
import numpy as np
import pynisher
from smac.scenario.scenario import Scenario
from smac.facade.smac_hpo_facade import SMAC4HPO
from smac.initial_design.latin_hypercube_design import LHDesign


PipelineTuneResult = namedtuple("PipelineTuneResult", ["inc_cfg", "cfgs", 
    "inc_cfgs", "costs", "inc_costs", "truedyn_costs", "inc_truedyn_costs", 
    "surr_trajs", "truedyn_trajs", "surr_tune_result"])
"""
PipelineTuneREsult contains information about a tuning process.

.. py:attribute:: inc_cfg
    
    The final tuned configuration.

.. py:attribute:: cfgs

    List of configurations. The configuration evaluated at each
    tuning iteration.

.. py:attribute:: inc_cfgs

    The incumbent (best found so far) configuration at each
    tuning iteration.

.. py:attribute:: costs

    The cost observed at each iteration of
    tuning iteration.

.. py:attribute:: inc_costs

    The incumbent score at each tuning iteration.

.. py:attribute:: truedyn_costs

    The true dynamics cost observed at each iteration of
    tuning iteration. None if true dynamics was not provided.

.. py:attribute:: inc_truedyn_costs

    The incumbent true dynamics cost observed at each iteration of
    tuning iteration. None if true dynamics was not provided.

.. py:attribute:: surr_trajs

    The trajectory simulated at each tuning iteration with respect to the
    surrogate model.

.. py:attribute:: truedyn_trajs

    The trajectory simulated at each tuning iteration with respect to the
    true dynamics. None if true dynamics are not provided.

.. py:attribute:: surr_tune_result

    The ModelTuneResult from tuning the surrogate model, for modes "autotune"
    and "autoselect".  None for other tuning modes.

"""

autoselect_factories = [MLPFactory, SINDyFactory, ApproximateGPModelFactory,
        ARXFactory, KoopmanFactory]


#@pynisher.enforce_limits(wall_time_in_s=300, grace_period_in_s=10)
class CfgEvaluator:
    def __init__(self, pipeline, task, surrogate, sysid_trajs, truedyn):
        self.pipeline = pipeline
        self.task = task
        self.surrogate = surrogate
        self.sysid_trajs = sysid_trajs
        self.truedyn = truedyn

    def __call__(self, cfg):
        pipeline = self.pipeline
        task = self.task
        surrogate = self.surrogate
        sysid_trajs = self.sysid_trajs
        truedyn = self.truedyn
        info = dict()
        try:
            controller, cost, model = pipeline(cfg, task, sysid_trajs)
            print("Simulating Surrogate Trajectory: ")
            try:
                controller.reset()
                if task.has_num_steps():
                    surr_traj = simulate(controller, task.get_init_obs(),
                           task.term_cond, sim_model=surrogate, 
                           max_steps=task.get_num_steps())
                else:
                    surr_traj = simulate(controller, task.get_init_obs(),
                           task.term_cond, sim_model=surrogate)
                cost = task.get_cost()
                surr_cost = cost(surr_traj)
                print("Surrogate Cost: ", surr_cost)
                print("Surrogate Final State: ", surr_traj[-1].obs)
                info["surr_cost"] = surr_cost
                info["surr_traj"] = (surr_traj.obs.tolist(), surr_traj.ctrls.tolist())
            except np.linalg.LinAlgError:
                surr_cost = np.inf
                info["surr_cost"] = surr_cost
                info["surr_traj"] = None
            
            if not truedyn is None:
                print("Simulating True Dynamics Trajectory")
                controller, _, _ = pipeline(cfg, task, sysid_trajs, model=model)
                controller.reset()
                if task.has_num_steps():
                    truedyn_traj = simulate(controller, task.get_init_obs(),
                       task.term_cond, dynamics=truedyn, max_steps=task.get_num_steps())
                else:
                    truedyn_traj = simulate(controller, task.get_init_obs(),
                       task.term_cond, dynamics=truedyn)
                truedyn_cost = cost(truedyn_traj)
                print("True Dynamics Cost: ", truedyn_cost)
                print("True Dynamics Final State: ", truedyn_traj[-1].obs)
                info["truedyn_cost"] = truedyn_cost
                info["truedyn_traj"] = (truedyn_traj.obs.tolist(), 
                        truedyn_traj.ctrls.tolist())
        except MPCCompatibilityError:
            surr_cost = np.inf

        return surr_cost, info

class PipelineTuner:
    """
    This class tunes SysID+MPC pipelines.
    """
    def __init__(self, surrogate_mode="defaultcfg", surrogate_factory=None, surrogate_split=None, surrogate_cfg=None,
            surrogate_evaluator=None, surrogate_tune_holdout=0.25, surrogate_tune_metric="rmse"):
        """
        Parameters
        ----------
        surrogate_mode : string
            Mode for selecting surrogate model. One of the following: "defaultcfg" - use the surrogate factories
            default configuration, "fixedcfg" - use the surrogate configuration passed by surrogate_cfg,
            "autotune" - automatically tune hyperparameters of surrogate, "autoselect" - automatically tune and
            select surrogate model, "pretrain" - Use an already trained surrogate which is passed when the tuning
            process is run.
        surrogate_factory : ModelFactory
            Factory for creating surrogate model. Required for all modes except for autoselect.
        surrogate_split : float
            Proportion of data to use for surrogate training.  Required for all modes except "pretrain"
        surrogate_cfg : Configuration
            Surrogate model config, required for "fixedcfg" mode
        surrogate_evaluator : ModelEvaluator
            Evaluator to use for surrogate tuning, used for "autoselect" and "autotune" modes. If not
            passed, will use HoldoutEvaluator with default arguments.
        surrogate_tune_holdout : float
            Proportion of data to hold out for surrogate tuning. Used for "autotune" and "autoselect" modes.
        surrogate_tune_metric : string
            Model metric to use for surrogate tuning.  Used for "autotune" and "autoselect" modes. See documentation
            of ModelEvaluator for more details.
        """
        self.surrogate_mode = surrogate_mode
        self.surrogate_factory = surrogate_factory
        self.surrogate_split = surrogate_split
        self.surrogate_cfg = surrogate_cfg
        self.surrogate_evaluator = surrogate_evaluator
        self.surrogate_tune_holdout = surrogate_tune_holdout
        self.surrogate_tune_metric = surrogate_tune_metric

    def _get_surrogate(self, pipeline, trajs, rng, surrogate_tune_iters):
        surrogate_tune_result = None
        if self.surrogate_mode == "defaultcfg":
            surrogate_cs = self.surrogate_factory.get_configuration_space()
            surrogate_cfg = surrogate_cs.get_default_configuration()
            surrogate = self.surrogate_factory(surrogate_cfg, trajs)
        elif self.surrogate_mode == "fixedcfg":
            surrogate = self.surrogate_factory(self.surrogate_cfg, trajs)
        elif self.surrogate_mode == "autotune":
            evaluator = self.surrogate_evaluator
            if evaluator is None:
                evaluator = HoldoutModelEvaluator(
                        holdout_prop = self.surrogate_tune_holdout,
                        metric = self.surrogate_tune_metric,
                        system=pipeline.system,
                        trajs=trajs,
                        rng=rng)
            model_tuner = ModelTuner(pipeline.system, evaluator) 
            model_tuner.add_model_factory(self.surrogate_factory)
            surrogate, surrogate_tune_result = model_tuner.run(rng, n_iters=surrogate_tune_iters) 
        elif self.surrogate_mode == "autoselect":
            evaluator = self.surrogate_evaluator
            if evaluator is None:
                evaluator = HoldoutModelEvaluator(
                        holdout_prop = self.surrogate_tune_holdout,
                        metric = self.surrogate_tune_metric,
                        system=pipeline.system,
                        trajs=trajs,
                        rng=rng)
            model_tuner = ModelTuner(pipeline.system, evaluator) 
            for factory in autoselect_factories:
                model_tuner.add_model_factory(factory(pipeline.system))
            surrogate, surrogate_tune_result = model_tuner.run(rng, n_iters=surrogate_tune_iters) 
        return surrogate, surrogate_tune_result



    def run(self, pipeline, task, trajs, n_iters, rng, surrogate=None, truedyn=None, 
            surrogate_tune_iters=100, special_debug=False):
        """
        Run tuning.

        Parameters
        ----------
        pipeline : Pipeline
            Pipeline to tune.

        task : Task
            Task specification to tune for

        trajs : List of Trajectory
            Trajectory training set.

        n_iters : int
            Number of tuning iterations

        rng : numpy.random.Generator
            RNG to use for tuning.

        surrogate : Model
            Surrogate model to use for tuning. Used for "pretrain" mode.

        truedyn : obs, ctrl -> obs
            True dynamics function. If passed, the true dynamics cost
            will be evaluated for each iteration in addition to the surrogate
            cost. However, this information will not be used for tuning.

        surrogate_tune_iters : int
            Number of iterations to use for surrogate tuning. Used for "autotune"
            and "autoselect" modes. Default is 100

        Returns
        -------
        controller : Controller
            Final tuned controller.

        tune_result : PipelineTuneResult
            Additional tuning information.
        """
        # Run surrogate training
        if surrogate is None:
            surr_size = int(self.surrogate_split * len(trajs))
            shuffled_trajs = trajs[:]
            rng.shuffle(shuffled_trajs)
            surr_trajs = shuffled_trajs[:surr_size]
            sysid_trajs = shuffled_trajs[surr_size:]

            surrogate, surr_tune_result = self._get_surrogate(pipeline, surr_trajs, rng, surrogate_tune_iters)
        else:
            sysid_trajs = trajs
            surr_tune_result = None

        if special_debug:
            with open("../../out/2021-05-17/surrogate.pkl", "wb") as f:
                pickle.dump(surrogate, f)
            eval_idx = [0]

        eval_kwargs = dict()
        eval_kwargs["pipeline"] = pipeline
        eval_kwargs["surrogate"] = surrogate
        eval_kwargs["task"] = task
        eval_kwargs["sysid_trajs"] = sysid_trajs
        eval_kwargs["truedyn"] = truedyn

        #eval_cfg = pynisher.enforce_limits(wall_time_in_s=10, 
        #        grace_period_in_s=10)(CfgEvaluator(**eval_kwargs))
        eval_cfg = CfgEvaluator(**eval_kwargs)
        eval_cfg_pickle = pickle.dumps(eval_cfg)
        print("Pickled Size: ", len(eval_cfg_pickle))
        cfg = pipeline.get_configuration_space().get_default_configuration()
        cfg["_model:model"] = "ARX"
        p = multiprocessing.Process(target=eval_cfg, args=(cfg,))
        p.start()
        p.join()
        #print("Eval Result: ", eval_cfg(cfg))

        smac_rng = np.random.RandomState(seed=rng.integers(1 << 31))
        scenario = Scenario({"run_obj" : "quality",
                             "runcount-limit" : n_iters,
                             "cs" : pipeline.get_configuration_space(),
                             "deterministic" : "true",
                             "limit_resources" : False,
                             #"cutoff" : 500,
                             "abort_on_first_run_crash" : False
                             })

        smac = SMAC4HPO(scenario=scenario, rng=smac_rng,
                initial_design=LHDesign,
                tae_runner=eval_cfg
                )

        inc_cfg = smac.optimize()

        cfgs, inc_cfgs, costs, inc_costs, truedyn_costs, inc_truedyn_costs, surr_trajs,\
                truedyn_trajs =  [], [], [], [], [], [], [], []
        inc_cost = float("inf")

        for key, val in smac.runhistory.data.items():
            cfg = smac.runhistory.ids_config[key.config_id]
            if not val.additional_info:
                continue
            if val.cost < inc_cost:
                inc_cost = val.cost
                if "truedyn_cost" in val.additional_info:
                    inc_truedyn_cost = val.additional_info["truedyn_cost"]
                inc_cfg = cfg
            inc_costs.append(inc_cost)
            inc_cfgs.append(inc_cfg)
            cfgs.append(cfg)
            costs.append(val.cost)
            if val.additional_info["surr_traj"] is not None:
                surr_obs, surr_ctrls = val.additional_info["surr_traj"]
                surr_traj = zeros(pipeline.system, len(surr_obs))
                surr_traj.obs[:] = surr_obs
                surr_traj.ctrls[:] = surr_ctrls
                surr_trajs.append(surr_traj)
            else:
                surr_trajs.append(None)
            if "truedyn_cost" in val.additional_info:
                inc_truedyn_costs.append(inc_truedyn_cost)
                truedyn_costs.append(val.additional_info["truedyn_cost"])
                truedyn_obs, truedyn_ctrls = val.additional_info["truedyn_traj"]
                truedyn_traj = zeros(pipeline.system, len(truedyn_obs))
                truedyn_traj.obs[:] = truedyn_obs
                truedyn_traj.ctrls[:] = truedyn_ctrls
                truedyn_trajs.append(truedyn_traj)

        tune_result = PipelineTuneResult(inc_cfg = inc_cfg,
                cfgs = cfgs,
                inc_cfgs = inc_cfgs,
                costs = costs,
                inc_costs = inc_costs,
                truedyn_costs = truedyn_costs,
                inc_truedyn_costs = inc_truedyn_costs,
                surr_trajs = surr_trajs,
                truedyn_trajs = truedyn_trajs,
                surr_tune_result = surr_tune_result)

        # Generate final model and controller
        controller, cost, model = pipeline(inc_cfg, task, sysid_trajs)

        return controller, tune_result
