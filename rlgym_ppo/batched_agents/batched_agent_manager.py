"""
File: batched_agent_manager.py
Author: Matthew Allen

Description:
    A class to manage the multi-processed agents interacting with instances of the environment. This class is responsible
    for spawning and closing the individual processes, interacting with them through their respective pipes, and organizing
    the trajectories from each instance of the environment.
"""

import multiprocessing as mp
import time
from typing import Union

import numpy as np
import torch

from rlgym_ppo.batched_agents import BatchedTrajectory
from rlgym_ppo.batched_agents.batched_agent import batched_agent_process
from rlgym_ppo.batched_agents import comm_consts


class BatchedAgentManager(object):
    def __init__(self, policy, min_inference_size=8, seed=123):
        self.policy = policy
        self.seed = seed
        self.processes = {}
        self.current_obs = []
        self.current_pids = []
        self.average_reward = None
        self.cumulative_timesteps = 0
        self.min_inference_size = min_inference_size
        self.ep_rews = {}
        self.trajectory_map = {}
        self.prev_time = 0
        self.completed_trajectories = []

        self.buffer_ptr = 0
        self.n_procs = 0

    def collect_timesteps(self, n):
        """
        Collect a specified number of timesteps from the environment.

        :param n: Number of timesteps to collect.
        :return: A tuple containing the collected data arrays and additional information.
                - states (np.ndarray): Array of states.
                - actions (np.ndarray): Array of actions.
                - log_probs (np.ndarray): Array of log probabilities of actions.
                - rewards (np.ndarray): Array of rewards.
                - next_states (np.ndarray): Array of next states.
                - dones (np.ndarray): Array of done flags.
                - truncated (np.ndarray): Array of truncated flags.
                - n_collected (int): Number of timesteps collected.
                - elapsed_time (float): Time taken to collect the timesteps.
        """

        t1 = time.perf_counter()
        states = []
        actions = []
        log_probs = []
        rewards = []
        next_states = []
        dones = []
        truncated = []

        n_collected = 0
        n_procs = max(1, len(list(self.processes.keys())))
        n_obs_per_inference = min(self.min_inference_size, n_procs)

        # Collect n timesteps.
        while n_collected < n:
            next_obs = []
            next_pids = []

            # Send actions for the current observations and collect new states to act on. Note that the next states
            # will not necessarily be from the environments that we just sent actions to. Whatever timestep data happens
            # to be lying around in the buffer will be collected and used in the next inference step.
            self._send_actions()
            n_collected += self._collect_responses(next_obs, next_pids, n_obs_per_inference)

            self.current_obs = next_obs
            self.current_pids = next_pids
            self._sync_trajectories()

        # Organize our new timesteps into the appropriate lists.
        for pid, trajectory in self.trajectory_map.items():
            self.completed_trajectories.append(trajectory)
            self.trajectory_map[pid] = BatchedTrajectory()

        for trajectory in self.completed_trajectories:
            trajectories = trajectory.get_all()
            if len(trajectories) == 0:
                continue

            for traj in trajectories:
                (
                    trajectory_states,
                    trajectory_actions,
                    trajectory_log_probs,
                    trajectory_rewards,
                    trajectory_next_states,
                    trajectory_dones,
                ) = traj
                trajectory_truncated = [0 for _ in range(len(trajectory_dones))]
                trajectory_truncated[-1] = 1 if trajectory_dones[-1] == 0 else 0
                states += trajectory_states
                actions += trajectory_actions
                log_probs += trajectory_log_probs
                rewards += trajectory_rewards
                next_states += trajectory_next_states
                dones += trajectory_dones
                truncated += trajectory_truncated

        self.cumulative_timesteps += n_collected
        t2 = time.perf_counter()

        return (
            (
                np.asarray(states),
                np.asarray(actions),
                np.asarray(log_probs),
                np.asarray(rewards),
                np.asarray(next_states),
                np.asarray(dones),
                np.asarray(truncated),
            ),
            n_collected,
            t2 - t1,
        )

    def _sync_trajectories(self):
        for pid, trajectory in self.trajectory_map.items():
            if trajectory.update():
                self.completed_trajectories.append(trajectory)
                self.trajectory_map[pid] = BatchedTrajectory()

    @torch.no_grad()
    def _send_actions(self):
        """
        Send actions to environment processes based on current observations.
        """
        if self.current_obs is None:
            return

        dimensions = []
        for i in range(len(self.current_obs)):
            dimensions.append(self.current_obs[i].shape[0])

        inference_batch = np.concatenate(self.current_obs, axis=0)
        actions, log_probs = self.policy.get_action(inference_batch)
        actions = actions.numpy().astype(np.float32)

        step = 0
        for proc_id, dim_0 in zip(self.current_pids, dimensions):
            process, parent_end, child_end = self.processes[proc_id]
            stop = step+dim_0

            state = inference_batch[step:stop]
            action = actions[step:stop]
            logp = log_probs[step:stop]

            parent_end.send_bytes(comm_consts.pack_message(comm_consts.POLICY_ACTIONS_HEADER) + action.tobytes())

            self.trajectory_map[proc_id].action = action
            self.trajectory_map[proc_id].log_prob = logp
            self.trajectory_map[proc_id].state = state

            step += dim_0

        self.current_obs = None

    def _collect_responses(self, next_obs, next_pids, n_obs_per_inference):
        """
        Collect responses from environment processes and update trajectory data.
        :param next_obs: List to store next observations.
        :param next_pids: List to store process IDs.
        :return: Number of responses collected.
        """

        n_collected = 0
        buffer_ptr = self.buffer_ptr
        processes = self.processes
        trajectory_map = self.trajectory_map
        ep_rews = self.ep_rews
        n_procs = self.n_procs
        
        while n_collected < n_obs_per_inference:
            proc_package = processes[buffer_ptr]
            proc_id = buffer_ptr
            process, parent_end, child_end = proc_package
            
            buffer_ptr += 1
            if buffer_ptr >= n_procs:
                buffer_ptr = 0
                
            if not parent_end.poll():
                continue

            available_data = parent_end.recv_bytes()
            message = np.frombuffer(available_data, dtype=np.float32)
            header = message[:comm_consts.HEADER_LEN]
            message = message[comm_consts.HEADER_LEN:]

            if header[0] == comm_consts.ENV_STEP_DATA_HEADER[0]:
                done = message[0]
                n_elements_in_state_shape = int(message[1])
                state_shape = [int(arg) for arg in message[2:2 + n_elements_in_state_shape]]
                if n_elements_in_state_shape == 1:
                    n_agents = 1
                    state_shape = [1, state_shape[0]]
                else:
                    n_agents = state_shape[0]

                rew_start = 2 + n_elements_in_state_shape
                rews = message[rew_start:rew_start + n_agents]
                next_observation = np.reshape(message[rew_start + n_agents:], state_shape)

                if n_agents > 1:
                    n_collected += n_agents
                    for i in range(n_agents):
                        if i >= len(ep_rews[proc_id]):
                            ep_rews[proc_id].append(rews[i])
                        else:
                            ep_rews[proc_id][i] += rews[i]
                else:
                    n_collected += 1
                    rews = rews[0]
                    ep_rews[proc_id][0] += rews

                if done:
                    if self.average_reward is None:
                        self.average_reward = self.ep_rews[proc_id][0]
                    else:
                        for ep_rew in self.ep_rews[proc_id]:
                            self.average_reward = self.average_reward * 0.9 + ep_rew * 0.1

                    ep_rews[proc_id] = [0]

                next_obs.append(next_observation)
                next_pids.append(proc_id)
                trajectory_map[proc_id].reward = rews
                trajectory_map[proc_id].next_state = next_observation
                trajectory_map[proc_id].done = done

        self.buffer_ptr = buffer_ptr

        return n_collected

    def _get_initial_states(self):
        """
        Retrieve initial states from environment processes.
        :return: None.
        """

        self.current_obs = []
        self.current_pids = []
        for proc_id, proc_package in self.processes.items():
            process, parent_end, child_end = proc_package
            while not parent_end.poll():
                time.sleep(0.01)

            available_data = parent_end.recv_bytes()
            message = comm_consts.unpack_message(available_data)

            header = message[:comm_consts.HEADER_LEN]
            data = message[comm_consts.HEADER_LEN:]

            if header == comm_consts.ENV_RESET_STATE_HEADER:
                self.current_pids.append(proc_id)

                n_elements_in_state_shape = int(data[0])
                shape = [int(arg) for arg in data[1:1+n_elements_in_state_shape]]
                if n_elements_in_state_shape == 1:
                    shape = [1, shape[0]]

                obs = np.reshape(data[1+n_elements_in_state_shape:], shape)
                self.current_obs.append(obs)

    def _get_env_shapes(self):
        """
        Retrieve environment observation and action space shapes from one of the connected environment processes.
        :return: A tuple containing observation shape, action shape, and action space type.
        """
        process, parent_end, child_end = self.processes[0]
        request_msg = comm_consts.pack_message(comm_consts.ENV_SHAPES_HEADER)
        parent_end.send_bytes(request_msg)

        obs_shape, action_shape, action_space_type = -1, -1, -1
        done = False

        while not done:
            while not parent_end.poll():
                time.sleep(0.1)

            available_data = parent_end.recv_bytes()
            message = comm_consts.unpack_message(available_data)
            header = message[:comm_consts.HEADER_LEN]
            data = message[comm_consts.HEADER_LEN:]

            if header == comm_consts.ENV_SHAPES_HEADER:
                obs_shape, action_shape, action_space_type = data
                done = True

        return int(obs_shape), int(action_shape), int(action_space_type)

    def init_processes(
        self,
        n_processes,
        build_env_fn,
        spawn_delay=None,
        render=False,
        render_delay: Union[float, None] = None,
    ):
        """
        Initialize and spawn environment processes.

        :param n_processes: Number of processes to spawn.
        :param build_env_fn: A function to build the environment for each process.
        :param spawn_delay: Delay between spawning processes. Defaults to None.
        :param render: Whether an environment should be rendered while collecting timesteps.
        :param render_delay: A period in seconds to delay a process between frames while rendering.
        :return: A tuple containing observation shape, action shape, and action space type.
        """

        can_fork = "forkserver" in mp.get_all_start_methods()
        start_method = "forkserver" if can_fork else "spawn"
        context = mp.get_context(start_method)
        self.n_procs = n_processes

        for i in range(n_processes):
            render_this_proc = i == 0 and render
            parent_end, child_end = context.Pipe()
            process = context.Process(
                target=batched_agent_process,
                args=(i, child_end, self.seed + i, render_this_proc, render_delay),
            )
            process.start()
            parent_end.send(("initialization_data", build_env_fn))
            self.processes[i] = (process, parent_end, child_end)
            if spawn_delay is not None:
                time.sleep(spawn_delay)

        self.ep_rews = {pid: [0] for pid in self.processes.keys()}
        self.trajectory_map = {
            pid: BatchedTrajectory() for pid in self.processes.keys()
        }
        self._get_initial_states()
        return self._get_env_shapes()

    def cleanup(self):
        """
        Clean up resources and terminate processes.
        """
        for proc_id, proc_package in self.processes.items():
            process, parent_end, child_end = proc_package
            try:
                parent_end.send_bytes(comm_consts.pack_message(comm_consts.STOP_MESSAGE_HEADER))
            except:
                import traceback
                print("Failed to send stop signal to child process!")
                traceback.print_exc()
            try:
                parent_end.close()
            except:
                print("Unable to close parent connection")
            try:
                child_end.close()
            except:
                print("Unable to close child connection")
            try:
                process.join()
            except:
                print("Unable to join process")
