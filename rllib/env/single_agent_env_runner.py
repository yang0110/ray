import gymnasium as gym
import numpy as np
import tree

from collections import defaultdict
from functools import partial
from typing import Dict, List, Optional, Tuple

from ray.rllib.algorithms.algorithm_config import AlgorithmConfig
from ray.rllib.algorithms.callbacks import DefaultCallbacks
from ray.rllib.core.models.base import STATE_IN, STATE_OUT
from ray.rllib.core.rl_module.rl_module import RLModule, SingleAgentRLModuleSpec
from ray.rllib.env.env_runner import EnvRunner
from ray.rllib.env.single_agent_episode import SingleAgentEpisode
from ray.rllib.env.utils import _gym_env_creator
from ray.rllib.evaluation.metrics import RolloutMetrics
from ray.rllib.policy.sample_batch import DEFAULT_POLICY_ID, SampleBatch
from ray.rllib.utils.annotations import ExperimentalAPI, override
from ray.rllib.utils.framework import try_import_tf, try_import_torch
from ray.rllib.utils.numpy import convert_to_numpy
from ray.rllib.utils.torch_utils import convert_to_torch_tensor
from ray.rllib.utils.typing import TensorStructType, TensorType
from ray.tune.registry import ENV_CREATOR, _global_registry


_, tf, _ = try_import_tf()
torch, nn = try_import_torch()


@ExperimentalAPI
class SingleAgentEnvRunner(EnvRunner):
    """The generic environment runner for the single agent case."""

    @override(EnvRunner)
    def __init__(self, config: AlgorithmConfig, **kwargs):
        super().__init__(config=config)

        # Get the worker index on which this instance is running.
        self.worker_index: int = kwargs.get("worker_index")

        # Create our callbacks object.
        self._callbacks: DefaultCallbacks = self.config.callbacks_class()

        # Create the vectorized gymnasium env.

        # Register env for the local context.
        # Note, `gym.register` has to be called on each worker.
        if isinstance(self.config.env, str) and _global_registry.contains(
            ENV_CREATOR, self.config.env
        ):
            entry_point = partial(
                _global_registry.get(ENV_CREATOR, self.config.env),
                self.config.env_config,
            )

        else:
            entry_point = partial(
                _gym_env_creator,
                env_context=self.config.env_config,
                env_descriptor=self.config.env,
            )
        gym.register("rllib-single-agent-env-runner-v0", entry_point=entry_point)

        # Wrap into `VectorListInfo`` wrapper to get infos as lists.
        self.env: gym.Wrapper = gym.wrappers.VectorListInfo(
            gym.vector.make(
                "rllib-single-agent-env-runner-v0",
                num_envs=self.config.num_envs_per_worker,
                asynchronous=self.config.remote_worker_envs,
            )
        )
        self.num_envs: int = self.env.num_envs
        assert self.num_envs == self.config.num_envs_per_worker

        self._callbacks.on_environment_created(
            env_runner=self,
            env=self.env,
            env_config=self.config.env_config,
        )

        # Create our own instance of the (single-agent) `RLModule` (which
        # the needs to be weight-synched) each iteration.
        try:
            module_spec: SingleAgentRLModuleSpec = (
                self.config.get_default_rl_module_spec()
            )
            module_spec.observation_space = self.env.envs[0].observation_space
            # TODO (simon): The `gym.Wrapper` for `gym.vector.VectorEnv` should
            #  actually hold the spaces for a single env, but for boxes the
            #  shape is (1, 1) which brings a problem with the action dists.
            #  shape=(1,) is expected.
            module_spec.action_space = self.env.envs[0].action_space
            module_spec.model_config_dict = self.config.model
            self.module: RLModule = module_spec.build()
        except NotImplementedError:
            self.module = None

        # This should be the default.
        self._needs_initial_reset: bool = True
        self._episodes: List[Optional["SingleAgentEpisode"]] = [
            None for _ in range(self.num_envs)
        ]

        self._done_episodes_for_metrics: List["SingleAgentEpisode"] = []
        self._ongoing_episodes_for_metrics: Dict[List] = defaultdict(list)
        self._ts_since_last_metrics: int = 0
        self._weights_seq_no: int = 0

        # TODO (sven): This is a temporary solution. STATE_OUTs
        #  will be resolved entirely as `extra_model_outputs` and
        #  not be stored separately inside Episodes.
        self._states = [None for _ in range(self.num_envs)]

    @override(EnvRunner)
    def sample(
        self,
        *,
        num_timesteps: int = None,
        num_episodes: int = None,
        explore: bool = True,
        random_actions: bool = False,
        with_render_data: bool = False,
    ) -> List["SingleAgentEpisode"]:
        """Runs and returns a sample (n timesteps or m episodes) on the env(s)."""
        assert not (num_timesteps is not None and num_episodes is not None)

        # If no execution details are provided, use the config to try to infer the
        # desired timesteps/episodes to sample.
        if (
            num_timesteps is None
            and num_episodes is None
            and self.config.batch_mode == "truncate_episodes"
        ):
            num_timesteps = (
                self.config.get_rollout_fragment_length(worker_index=self.worker_index)
                * self.num_envs
            )

        # Sample n timesteps.
        if num_timesteps is not None:
            samples = self._sample_timesteps(
                num_timesteps=num_timesteps,
                explore=explore,
                random_actions=random_actions,
                force_reset=False,
            )
        # Sample m episodes.
        elif num_episodes is not None:
            samples = self._sample_episodes(
                num_episodes=num_episodes,
                explore=explore,
                random_actions=random_actions,
                with_render_data=with_render_data,
            )
        # For complete episodes mode, sample as long as the number of timesteps
        # done is smaller than the `train_batch_size`.
        else:
            total = 0
            samples = []
            while total < self.config.train_batch_size:
                episodes = self._sample_episodes(
                    num_episodes=self.num_envs,
                    explore=explore,
                    random_actions=random_actions,
                    with_render_data=with_render_data,
                )
                total += sum(len(e) for e in episodes)
                samples.extend(episodes)

        # Make the `on_sample_end` callback.
        self._callbacks.on_sample_end(env_runner=self, samples=samples)

        return samples

    def _sample_timesteps(
        self,
        num_timesteps: int,
        explore: bool = True,
        random_actions: bool = False,
        force_reset: bool = False,
    ) -> List[SingleAgentEpisode]:
        """Helper method to sample n timesteps."""

        done_episodes_to_return: List[SingleAgentEpisode] = []

        # Get initial states for all 'batch_size_B` rows in the forward batch,
        # i.e. for all vector sub_envs.
        if hasattr(self.module, "get_initial_state"):
            initial_states = tree.map_structure(
                lambda s: np.repeat(s, self.num_envs, axis=0),
                self.module.get_initial_state(),
            )
        else:
            initial_states = {}

        # Have to reset the env (on all vector sub_envs).
        if force_reset or self._needs_initial_reset:
            # Create n new episodes and make the `on_episode_created` callbacks.
            self._episodes = []
            for env_index in range(self.num_envs):
                self._episodes.append(SingleAgentEpisode())
                self._make_on_episode_callback("on_episode_created", env_index)

            obs, infos = self.env.reset()

            # Call `on_episode_start()` callbacks.
            for env_index in range(self.num_envs):
                self._make_on_episode_callback("on_episode_start", env_index)

            # We just reset the env. Don't have to force this again in the next
            # call to `self._sample_timesteps()`.
            self._needs_initial_reset = False

            states = initial_states

            # Set initial obs and states in the episodes.
            for env_index in range(self.num_envs):
                # TODO (sven): Maybe move this into connector pipeline
                # (even if automated).
                self._episodes[env_index].add_env_reset(
                    observation=obs[env_index],
                    infos=infos[env_index],
                )
                self._states[env_index] = {k: s[env_index] for k, s in states.items()}
        # Do not reset envs, but instead continue in already started episodes.
        else:
            # Pick up stored observations and states from previous timesteps.
            obs = np.stack([eps.observations[-1] for eps in self._episodes])
            # Compile the initial state for each batch row (vector sub_env):
            # If episode just started, use the model's initial state, in the
            # other case use the state stored last in the Episode.
            states = {
                k: np.stack(
                    [
                        initial_states[k][env_index] if state is None else state[k]
                        for env_index, state in enumerate(self._states)
                    ]
                )
                for k in initial_states.keys()
            }

        # Loop through env in enumerate.(self._episodes):
        ts = 0

        while ts < num_timesteps:
            # Act randomly.
            if random_actions:
                actions = self.env.action_space.sample()
                action_logp = np.zeros(shape=(actions.shape[0],))
                fwd_out = {}
            # Compute an action using the RLModule.
            else:
                # Note, RLModule `forward()` methods expect `NestedDict`s.
                batch = {
                    STATE_IN: tree.map_structure(
                        lambda s: self._convert_from_numpy(s),
                        states,
                    ),
                    SampleBatch.OBS: self._convert_from_numpy(obs),
                }
                from ray.rllib.utils.nested_dict import NestedDict

                batch = NestedDict(batch)

                # Explore or not.
                if explore:
                    fwd_out = self.module.forward_exploration(batch)
                else:
                    fwd_out = self.module.forward_inference(batch)

                # TODO (sven): Will be completely replaced by connector logic in
                #  upcoming PR.
                actions, action_logp = self._sample_actions_if_necessary(
                    fwd_out, explore
                )

                fwd_out = convert_to_numpy(fwd_out)

                if STATE_OUT in fwd_out:
                    states = fwd_out[STATE_OUT]

            obs, rewards, terminateds, truncateds, infos = self.env.step(actions)

            ts += self.num_envs

            for env_index in range(self.num_envs):
                # TODO (sven): Will be replaced soon by RLlib's default
                #  ConnectorV2 in near future PR.
                # Extract state for vector sub_env.
                s_env_index = {k: s[env_index] for k, s in states.items()}
                # The last entry in self.observations[i] is already the reset
                # obs of the new episode.
                # TODO (simon): This might be unfortunate if a user needs to set a
                # certain env parameter during different episodes (for example for
                # benchmarking).
                extra_model_output = {}
                for k, v in fwd_out.items():
                    if SampleBatch.ACTIONS != k:
                        extra_model_output[k] = v[env_index]
                # TODO (simon, sven): Some algos do not have logps.
                extra_model_output[SampleBatch.ACTION_LOGP] = action_logp[env_index]

                # In inference we have only the action logits.
                if terminateds[env_index] or truncateds[env_index]:
                    # Finish the episode with the actual terminal observation stored in
                    # the info dict.
                    self._episodes[env_index].add_env_step(
                        # Gym vector env provides the `"final_observation"`.
                        infos[env_index]["final_observation"],
                        actions[env_index],
                        rewards[env_index],
                        infos=infos[env_index]["final_info"],
                        terminated=terminateds[env_index],
                        truncated=truncateds[env_index],
                        extra_model_outputs=extra_model_output,
                    )
                    self._states[env_index] = s_env_index

                    # Make the `on_episode_step` callback (before finalizing the
                    # episode object).
                    self._make_on_episode_callback("on_episode_step", env_index)

                    # Reset h-states to the model's intiial ones b/c we are starting a
                    # new episode.
                    if hasattr(self.module, "get_initial_state"):
                        for k, v in self.module.get_initial_state().items():
                            states[k][env_index] = convert_to_numpy(v)

                    done_episodes_to_return.append(self._episodes[env_index].finalize())

                    # Make the `on_episode_env` callback (after having finalized the
                    # episode object).
                    self._make_on_episode_callback("on_episode_end", env_index)

                    # Create a new episode object with already the reset data in it.
                    self._episodes[env_index] = SingleAgentEpisode(
                        observations=[obs[env_index]], infos=[infos[env_index]]
                    )
                    # Make the `on_episode_start` callback.
                    self._make_on_episode_callback("on_episode_start", env_index)

                    self._states[env_index] = s_env_index
                else:
                    self._episodes[env_index].add_env_step(
                        obs[env_index],
                        actions[env_index],
                        rewards[env_index],
                        infos=infos[env_index],
                        extra_model_outputs=extra_model_output,
                    )
                    # Make the `on_episode_step` callback.
                    self._make_on_episode_callback("on_episode_step", env_index)
                    self._states[env_index] = s_env_index

        # Return done episodes ...
        self._done_episodes_for_metrics.extend(done_episodes_to_return)
        # Also, make sure, we return a copy and start new chunks so that callers
        # of this function do not alter the ongoing and returned Episode objects.
        new_episodes = [eps.cut() for eps in self._episodes]

        # ... and all ongoing episode chunks.
        # Initialized episodes do not have recorded any step and lack
        # `extra_model_outputs`.
        ongoing_episodes_to_return = [
            episode.finalize() for episode in self._episodes if episode.t > 0
        ]
        for eps in ongoing_episodes_to_return:
            self._ongoing_episodes_for_metrics[eps.id_].append(eps)

        # Record last metrics collection.
        self._ts_since_last_metrics += ts

        self._episodes = new_episodes

        # Make the `on_sample_end` callback.
        samples = done_episodes_to_return + ongoing_episodes_to_return

        return samples

    def _sample_episodes(
        self,
        num_episodes: int,
        explore: bool = True,
        random_actions: bool = False,
        with_render_data: bool = False,
    ) -> List["SingleAgentEpisode"]:
        """Helper method to run n episodes.

        See docstring of `self.sample()` for more details.
        """
        # If user calls sample(num_timesteps=..) after this, we must reset again
        # at the beginning.
        self._needs_initial_reset = True

        done_episodes_to_return: List["SingleAgentEpisode"] = []

        obs, infos = self.env.reset()
        episodes = []
        for env_index in range(self.num_envs):
            episodes.append(SingleAgentEpisode())
            self._make_on_episode_callback("on_episode_created", env_index, episodes)

        # Get initial states for all 'batch_size_B` rows in the forward batch,
        # i.e. for all vector sub_envs.
        if hasattr(self.module, "get_initial_state"):
            states = tree.map_structure(
                lambda s: np.repeat(s, self.num_envs, axis=0),
                self.module.get_initial_state(),
            )
        else:
            states = {}

        render_images = [None] * self.num_envs
        if with_render_data:
            render_images = [e.render() for e in self.env.envs]

        for env_index in range(self.num_envs):
            episodes[env_index].add_env_reset(
                observation=obs[env_index],
                infos=infos[env_index],
                render_image=render_images[env_index],
            )
            self._make_on_episode_callback("on_episode_start", env_index, episodes)

        eps = 0
        while eps < num_episodes:
            if random_actions:
                actions = self.env.action_space.sample()
                action_logp = np.zeros(shape=(actions.shape[0]))
                fwd_out = {}
            else:
                batch = {
                    # TODO (sven): This will move entirely into connector logic in
                    #  upcoming PR.
                    STATE_IN: tree.map_structure(
                        lambda s: self._convert_from_numpy(s), states
                    ),
                    SampleBatch.OBS: self._convert_from_numpy(obs),
                }

                # Explore or not.
                if explore:
                    fwd_out = self.module.forward_exploration(batch)
                else:
                    fwd_out = self.module.forward_inference(batch)

                # TODO (sven): This will move entirely into connector logic in upcoming
                #  PR.
                actions, action_logp = self._sample_actions_if_necessary(
                    fwd_out, explore
                )

                fwd_out = convert_to_numpy(fwd_out)

                # TODO (sven): This will move entirely into connector logic in upcoming
                #  PR.
                if STATE_OUT in fwd_out:
                    states = convert_to_numpy(fwd_out[STATE_OUT])

            obs, rewards, terminateds, truncateds, infos = self.env.step(actions)
            if with_render_data:
                render_images = [e.render() for e in self.env.envs]

            for env_index in range(self.num_envs):
                # Extract info and state for vector sub_env.
                # info = {k: v[i] for k, v in infos.items()}
                # The last entry in self.observations[i] is already the reset
                # obs of the new episode.
                extra_model_output = {}
                for k, v in fwd_out.items():
                    if SampleBatch.ACTIONS not in k:
                        extra_model_output[k] = v[env_index]
                # TODO (sven): This will move entirely into connector logic in upcoming
                #  PR.
                extra_model_output[SampleBatch.ACTION_LOGP] = action_logp[env_index]

                if terminateds[env_index] or truncateds[env_index]:
                    eps += 1

                    episodes[env_index].add_env_step(
                        infos[env_index]["final_observation"],
                        actions[env_index],
                        rewards[env_index],
                        infos=infos[env_index]["final_info"],
                        terminated=terminateds[env_index],
                        truncated=truncateds[env_index],
                        extra_model_outputs=extra_model_output,
                    )
                    # Make `on_episode_step` callback before finalizing the episode.
                    self._make_on_episode_callback(
                        "on_episode_step", env_index, episodes
                    )
                    done_episodes_to_return.append(episodes[env_index].finalize())

                    # Make `on_episode_end` callback after finalizing the episode.
                    self._make_on_episode_callback(
                        "on_episode_end", env_index, episodes
                    )

                    # Also early-out if we reach the number of episodes within this
                    # for-loop.
                    if eps == num_episodes:
                        break

                    # TODO (sven): This will move entirely into connector logic in
                    #  upcoming PR.
                    if hasattr(self.module, "get_initial_state"):
                        for k, v in self.module.get_initial_state().items():
                            states[k][env_index] = (convert_to_numpy(v),)

                    # Create a new episode object.
                    episodes[env_index] = SingleAgentEpisode(
                        observations=[obs[env_index]],
                        infos=[infos[env_index]],
                        render_images=None
                        if render_images[env_index] is None
                        else [render_images[env_index]],
                    )
                    # Make `on_episode_start` callback.
                    self._make_on_episode_callback(
                        "on_episode_start", env_index, episodes
                    )
                else:
                    episodes[env_index].add_env_step(
                        obs[env_index],
                        actions[env_index],
                        rewards[env_index],
                        infos=infos[env_index],
                        render_image=render_images[env_index],
                        extra_model_outputs=extra_model_output,
                    )
                    # Make `on_episode_step` callback.
                    self._make_on_episode_callback(
                        "on_episode_step", env_index, episodes
                    )

        self._done_episodes_for_metrics.extend(done_episodes_to_return)
        self._ts_since_last_metrics += sum(len(eps) for eps in done_episodes_to_return)

        # Initialized episodes have to be removed as they lack `extra_model_outputs`.
        samples = [episode for episode in done_episodes_to_return if episode.t > 0]

        return samples

    def _make_on_episode_callback(self, which: str, idx: int, episodes=None):
        episodes = episodes if episodes is not None else self._episodes
        getattr(self._callbacks, which)(
            episode=episodes[idx],
            env_runner=self,
            env=self.env,
            rl_module=self.module,
            env_index=idx,
        )

    # TODO (sven): Remove the requirement for EnvRunners/RolloutWorkers to have this
    #  API. Instead Algorithm should compile episode metrics itself via its local
    #  buffer.
    def get_metrics(self) -> List[RolloutMetrics]:
        # Compute per-episode metrics (only on already completed episodes).
        metrics = []
        for eps in self._done_episodes_for_metrics:
            assert eps.is_done
            episode_length = len(eps)
            episode_reward = eps.get_return()
            # Don't forget about the already returned chunks of this episode.
            if eps.id_ in self._ongoing_episodes_for_metrics:
                for eps2 in self._ongoing_episodes_for_metrics[eps.id_]:
                    episode_length += len(eps2)
                    episode_reward += eps2.get_return()
                del self._ongoing_episodes_for_metrics[eps.id_]

            metrics.append(
                RolloutMetrics(
                    episode_length=episode_length,
                    episode_reward=episode_reward,
                )
            )

        self._done_episodes_for_metrics.clear()
        self._ts_since_last_metrics = 0

        return metrics

    # TODO (sven): Remove the requirement for EnvRunners/RolloutWorkers to have this
    #  API. Replace by proper state overriding via `EnvRunner.set_state()`
    def set_weights(self, weights, global_vars=None, weights_seq_no: int = 0):
        """Writes the weights of our (single-agent) RLModule."""

        if isinstance(weights, dict) and DEFAULT_POLICY_ID in weights:
            weights = weights[DEFAULT_POLICY_ID]
        weights = self._convert_to_tensor(weights)
        self.module.set_state(weights)

        # Check, if an update happened since the last call. See
        # `Algorithm._evaluate_async_with_env_runner`.
        # if self._weights_seq_no == 0 or self._weights_seq_no < weights_seq_no:
        #     # In case of a `StateDict` we have to extract the `
        #     # default_policy`.
        #     # TODO (sven): Handle this probably in `RLModule` as the latter
        #     #  does not need a 'StateDict' in its `set_state()` method
        #     #  as the `keras.Model.base_layer` has weights as `List[TensorType]`.
        #     self._weights_seq_no = weights_seq_no
        #     if isinstance(weights, dict) and DEFAULT_POLICY_ID in weights:
        #         weights = weights[DEFAULT_POLICY_ID]
        #     weights = self._convert_to_tensor(weights)
        #     self.module.set_state(weights)
        # # Otherwise ignore.
        # else:
        #     pass

    def get_weights(self, modules=None):
        """Returns the weights of our (single-agent) RLModule."""

        return self.module.get_state()

    @override(EnvRunner)
    def assert_healthy(self):
        # Make sure, we have built our gym.vector.Env and RLModule properly.
        assert self.env and self.module

    @override(EnvRunner)
    def stop(self):
        # Close our env object via gymnasium's API.
        self.env.close()

    # TODO (sven): Replace by default "to-env" connector.
    def _sample_actions_if_necessary(
        self, fwd_out: TensorStructType, explore: bool = True
    ) -> Tuple[np.array, np.array]:
        """Samples actions from action distribution if necessary."""

        # TODO (sven): Move this into connector pipeline (if no
        #  "actions" key in returned dict, sample automatically as
        #  the last piece of the connector pipeline; basically do
        #  the same thing that the Policy is currently doing, but
        #  using connectors)
        # If actions are provided just load them.
        if SampleBatch.ACTIONS in fwd_out.keys():
            actions = convert_to_numpy(fwd_out[SampleBatch.ACTIONS])
            # TODO (simon, sven): Some algos do not return logps.
            action_logp = convert_to_numpy(fwd_out[SampleBatch.ACTION_LOGP])
        # If no actions are provided we need to sample them.
        else:
            # Explore or not.
            if explore:
                action_dist_cls = self.module.get_exploration_action_dist_cls()
            else:
                action_dist_cls = self.module.get_inference_action_dist_cls()
            # Generate action distribution and sample actions.
            action_dist = action_dist_cls.from_logits(
                fwd_out[SampleBatch.ACTION_DIST_INPUTS]
            )
            actions = action_dist.sample()
            # We need numpy actions for gym environments.
            action_logp = convert_to_numpy(action_dist.logp(actions))
            actions = convert_to_numpy(actions)

        return actions, action_logp

    def _convert_from_numpy(self, array: np.array) -> TensorType:
        """Converts a numpy array to a framework-specific tensor."""

        if self.config.framework_str == "torch":
            return torch.from_numpy(array)
        else:
            return tf.convert_to_tensor(array)

    def _convert_to_tensor(self, struct) -> TensorType:
        """Converts structs to a framework-specific tensor."""

        if self.config.framework_str == "torch":
            return convert_to_torch_tensor(struct)
        else:
            return tree.map_structure(tf.convert_to_tensor, struct)
