import numpy as np
from .surrogate_evaluator import SurrogateEvaluator
from .control_evaluator import ControlEvaluator, NormalDistribution, ConstantDistribution
from ..utils import simulate


class BootstrapSurrogateEvaluator(SurrogateEvaluator):
    def __init__(self, system, tasks, trajs, surrogate, rng=None, n_bootstraps=10, surrogate_cfg=None,
                    surrogate_mode="defaultcfg", surrogate_tune_iters=100):
        ControlEvaluator.__init__(self, system, tasks, trajs)
        self.surrogate_mode = surrogate_mode
        self.surrogate_factory = surrogate_factory
        self.surr_trajs = trajs

        self._prepare_surrogate(trajs, rng=rng, surrogate_tune_iters=surrogate_tune_iters)

        self.bootstrap_models = self._get_bootstrap_models(rng, n_bootstraps)

    @property
    def is_parallelizable(self):
        return True

    def num_jobs(self):
        return len(self.bootstrap_models)

    def _get_bootstrap_models(self, rng, n_bootstraps):
        population = np.empty(len(self.surr_trajs), dtype=object)
        for i, traj in enumerate(self.surr_trajs):
            population[i] = traj
        bootstrap_surrogates = []
        for i in range(n_bootstraps):
            bootstrap_sample = rng.choice(population, len(population), replace=True, axis=0)
            bootstrap_surrogate = self.surrogate.clone()
            bootstrap_surrogate.train(bootstrap_sample)
            bootstrap_surrogates.append(bootstrap_surrogate)
        return bootstrap_surrogates

    def _run_surrogate(self, surrogate, controller):
        try:
            controller.reset()
            if self.task.has_num_steps():
                surr_traj = simulate(controller, self.task.get_init_obs(),
                    self.task.term_cond, sim_model=surrogate, 
                    ctrl_bounds=self.task.get_ctrl_bounds(),
                    max_steps=self.task.get_num_steps())
            else:
                surr_traj = simulate(controller, self.task.get_init_obs(),
                    ctrl_bounds=self.task.get_ctrl_bounds(),
                    term_cond=self.task.term_cond, sim_model=surrogate)
            cost = self.task.get_cost()
            surr_cost = cost(surr_traj)
            print("Surrogate Cost: ", surr_cost)
            print("Surrogate Final State: ", surr_traj[-1].obs)
            return surr_cost, (surr_traj.obs.tolist(), surr_traj.ctrls.tolist())
        except np.linalg.LinAlgError:
            return np.inf, None

    def set_device(self, device):
        for model in self.bootstrap_models:
            model.set_device(device)

    def run_job(self, controller, job_idx):
        surrogate = self.bootstrap_models[job_idx]
        print("Simulating Surrogate Trajectory for Model {}: ".format(job_idx))
        surr_cost, surr_traj = self._run_surrogate(surrogate, controller)
        result = {"surr_cost" : surr_cost, "surr_traj" : surr_traj}
        return result

    def aggregate_results(self, results):
        info = dict()
        info["surr_costs"] = []
        info["surr_trajs"] = []
        for result in results:
            info["surr_costs"].append(result["surr_cost"])
            info["surr_trajs"].append(result["surr_traj"])

        mean = np.mean(info["surr_costs"])
        std = np.std(info["surr_costs"])
        if not np.allclose(std, 0.0):
            distribution = NormalDistribution(mean, std)
        else:
            distribution = ConstantDistribution(mean)

        print("Surrogate Distribution: Mean {:.2f} Stddev {:.2f}".format(mean, std))

        return distribution, info

    def __call__(self, controller):
        results = [self.run_job(controller, i) for i in range(self.num_jobs())]

        return self.aggregate_results(results)
