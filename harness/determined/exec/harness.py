import argparse
import contextlib
import faulthandler
import logging
import sys
from typing import Iterator, Optional, Type

import determined as det
from determined import core, horovod, load
from determined.common.api import analytics, certs

logger = logging.getLogger("determined")


@contextlib.contextmanager
def maybe_periodic_stacktraces(debug_enabled: bool) -> Iterator[None]:
    if debug_enabled:
        faulthandler.dump_traceback_later(30, repeat=True)
    try:
        yield
    finally:
        if debug_enabled:
            faulthandler.cancel_dump_traceback_later()


def main(train_entrypoint: str) -> int:
    info = det.get_cluster_info()
    assert info is not None, "must be run on-cluster"
    assert info.task_type == "TRIAL", f'must be run with task_type="TRIAL", not "{info.task_type}"'

    # TODO: refactor profiling to to not use the cli_cert.
    certs.cli_cert = certs.default_load(info.master_url)

    trial_class = load.trial_class_from_entrypoint(train_entrypoint)

    if info.container_rank == 0:
        try:
            analytics.send_analytics("trial_loaded", analytics.get_trial_analytics(trial_class))
        except Exception as e:
            logger.debug(f"Cannot send analytics: {e}")

    # We can't import pytorch directly because if running TfKerasTrials with an image that contains
    # both torch and keras, keras will throw exceptions due to unexpected CUDNN library versions.
    if hasattr(det, "pytorch") and issubclass(trial_class, det.pytorch.PyTorchTrial):
        return _run_pytorch_trial(trial_class, info)

    # TODO: Don't include EnvContext object in the future high-level APIs for PyTorch or Keras.
    # It was natural to create this big-blob-of-config object, but it was a mistake to pass it into
    # the lowest layers of the harness code; it's too large of an object to be easily mockable,
    # which is part of why building local training mode has always been a challenge.
    #
    # A better pattern is to pass in exactly the information that is necessary at each layer.  We
    # will use that pattern for the future high-level APIs, but it's not worth refactoring e.g. the
    # TFKerasTrialController or EstimatorTrialController to add that functionality, so for now we
    # continue with the legacy strategy.

    env = det.EnvContext(
        master_url=info.master_url,
        master_cert_file=info.master_cert_file,
        master_cert_name=info.master_cert_name,
        experiment_config=info.trial._config,
        hparams=info.trial.hparams,
        latest_checkpoint=info.latest_checkpoint,
        steps_completed=info.trial._steps_completed,
        use_gpu=bool(info.gpu_uuids),
        container_gpus=info.gpu_uuids,
        slot_ids=info.slot_ids,
        debug=info.trial._debug,
        det_trial_id=str(info.trial.trial_id),
        det_experiment_id=str(info.trial.experiment_id),
        det_agent_id=info.agent_id,
        det_cluster_id=info.cluster_id,
        trial_seed=info.trial.trial_seed,
        trial_run_id=info.trial._trial_run_id,
        allocation_id=info.allocation_id,
        managed_training=True,
        test_mode=False,
        on_cluster=True,
    )

    det.common.set_logger(env.debug)
    logger.debug("Starting harness.")

    with maybe_periodic_stacktraces(env.debug):
        # Step 1: Load user code.
        # We can't build a core.Context without rank information, and we can't gather rank
        # information until the distributed backend is initialized, and we can't initialize the
        # correct distributed backend until we know which Trial class the user implemented.
        controller_class = load.get_trial_controller_class(trial_class)

        # Step 2: Initialize framework-specific details (dtrain framework, random seeds, etc).
        distributed_backend = det._DistributedBackend()
        controller_class.pre_execute_hook(env, distributed_backend)

        # Step 3: Now that the dtrain framework is initialized, build the DistributedContext object.
        # For harness.py, we only support a fixed set of Determined-provided launch layers, since
        # the TrialControllers only support a fixed set of launch layers.
        distributed = None
        if distributed_backend.use_horovod():
            distributed = core.DistributedContext.from_horovod(horovod.hvd)
        elif distributed_backend.use_deepspeed():
            distributed = core.DistributedContext.from_deepspeed()
        elif distributed_backend.use_torch():
            distributed = core.DistributedContext.from_torch_distributed()
        elif len(info.container_addrs) > 1 or len(info.slot_ids) > 1:
            raise ValueError(
                "In multi-slot tasks, the determined.exec.harness module must not be invoked "
                "directly.  Instead, it must be wrapped in one of the following launch layers: "
                "determined.launch.horovod, determined.launch.deepspeed"
            )

        # Step 4: Let core.init() create the core.Context.
        with core.init(
            distributed=distributed,
            preempt_mode=core.PreemptMode.ChiefOnly,
            tensorboard_mode=core.TensorboardMode.MANUAL,
        ) as core_context:
            trial_context = trial_class.trial_context_class(core_context, env)

            # Step 4: Instantiate the user's Trial.
            trial_inst = trial_class(trial_context)

            # Step 5: Create a TrialController and execute training
            logger.info(f"Creating {controller_class.__name__} with {trial_class.__name__}.")
            controller = controller_class.from_trial(
                trial_inst=trial_inst,
                context=trial_context,
                env=env,
            )

            controller.run()

    return 0


def _run_pytorch_trial(
    trial_class: "Type[det.pytorch.PyTorchTrial]",
    info: det.ClusterInfo,
) -> int:
    from determined import pytorch

    det.common.set_logger(info.trial._debug)

    logger.debug("Starting harness.")

    with maybe_periodic_stacktraces(info.trial._debug):
        with pytorch.init(
            hparams=info.trial.hparams,
            exp_conf=info.trial._config,
            aggregation_frequency=int(info.trial._config["optimizations"]["aggregation_frequency"]),
        ) as train_context:
            fp16_compression = bool(info.trial._config["optimizations"]["gradient_compression"])
            average_aggregated_gradients = bool(
                info.trial._config["optimizations"]["average_aggregated_gradients"]
            )

            train_context._set_default_gradient_compression(fp16_compression)
            train_context._set_default_average_aggregated_gradients(average_aggregated_gradients)
            train_context._set_is_pre_trainer()

            trial_inst = trial_class(train_context)

            if train_context.distributed.size > 1 and not train_context.distributed.rank == 0:
                log_level = logging.DEBUG if info.trial._debug else logging.WARNING
                logging.getLogger().setLevel(log_level)

            logger.info(
                f"Creating {pytorch._PyTorchTrialController.__name__} with {trial_class.__name__}."
            )

            trainer = pytorch.Trainer(trial_inst, train_context)

            trainer.configure_profiler(
                sync_timings=bool(info.trial._config["profiling"]["sync_timings"]),
                enabled=bool(info.trial._config["profiling"]["enabled"]),
                begin_on_batch=info.trial._config["profiling"]["begin_on_batch"],
                end_after_batch=info.trial._config["profiling"]["end_after_batch"],
            )

            if "global_batch_size" in info.trial.hparams:
                global_batch_size = int(
                    info.trial.hparams["global_batch_size"]
                )  # type: Optional[int]
            else:
                global_batch_size = None

            trainer.fit(
                checkpoint_period=pytorch.TrainUnit._from_values(
                    **info.trial._config["min_checkpoint_period"],
                    global_batch_size=global_batch_size,
                ),
                validation_period=pytorch.TrainUnit._from_values(
                    **info.trial._config["min_validation_period"],
                    global_batch_size=global_batch_size,
                ),
                reporting_period=pytorch.Batch(info.trial._config["scheduling_unit"]),
                checkpoint_policy=info.trial._config["checkpoint_policy"],
                latest_checkpoint=info.latest_checkpoint,
                step_zero_validation=info.trial._config["perform_initial_validation"],
                test_mode=False,
            )

    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("train_entrypoint")
    args = parser.parse_args()
    sys.exit(main(args.train_entrypoint))
