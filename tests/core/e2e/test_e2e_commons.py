from __future__ import annotations

import collections.abc
import glob
import os
from pathlib import Path

import yaml
from tensorboard.backend.event_processing.event_accumulator import EventAccumulator

from fairchem.core._cli import Runner
from fairchem.core.common.flags import flags
from fairchem.core.common.test_utils import (
    PGConfig,
    init_env_rank_and_launch_test,
    spawn_multi_process,
)
from fairchem.core.common.utils import build_config


def oc20_lmdb_train_and_val_from_paths(
    train_src, val_src, test_src=None, otf_norms=False
):
    datasets = {}
    if train_src is not None:
        datasets["train"] = {
            "src": train_src,
            "format": "lmdb",
            "key_mapping": {"y": "energy", "force": "forces"},
        }
        if otf_norms is True:
            datasets["train"].update(
                {
                    "transforms": {
                        "element_references": {
                            "fit": {
                                "targets": ["energy"],
                                "batch_size": 4,
                                "num_batches": 10,
                                "driver": "gelsd",
                            }
                        },
                        "normalizer": {
                            "fit": {
                                "targets": {"energy": None, "forces": {"mean": 0.0}},
                                "batch_size": 4,
                                "num_batches": 10,
                            }
                        },
                    }
                }
            )
        else:
            datasets["train"].update(
                {
                    "transforms": {
                        "normalizer": {
                            "energy": {
                                "mean": -0.7554450631141663,
                                "stdev": 2.887317180633545,
                            },
                            "forces": {"mean": 0.0, "stdev": 2.887317180633545},
                        }
                    }
                }
            )
    if val_src is not None:
        datasets["val"] = {"src": val_src, "format": "lmdb"}
    if test_src is not None:
        datasets["test"] = {"src": test_src, "format": "lmdb"}
    return datasets


def get_tensorboard_log_files(logdir):
    return glob.glob(f"{logdir}/tensorboard/*/events.out*")


def get_tensorboard_log_values(logdir):
    tf_event_files = get_tensorboard_log_files(logdir)
    assert len(tf_event_files) == 1
    tf_event_file = tf_event_files[0]
    acc = EventAccumulator(tf_event_file)
    acc.Reload()
    return acc


def merge_dictionary(d, u):
    for k, v in u.items():
        if isinstance(v, collections.abc.Mapping):
            d[k] = merge_dictionary(d.get(k, {}), v)
        else:
            d[k] = v
    return d

def _run_main(
    rundir,
    input_yaml,
    update_dict_with=None,
    update_run_args_with=None,
    save_checkpoint_to=None,
    save_predictions_to=None,
    world_size=0,
):
    config_yaml = Path(rundir) / "train_and_val_on_val.yml"

    with open(input_yaml) as yaml_file:
        yaml_config = yaml.safe_load(yaml_file)
    if update_dict_with is not None:
        yaml_config = merge_dictionary(yaml_config, update_dict_with)
        yaml_config["backend"] = "gloo"
    with open(str(config_yaml), "w") as yaml_file:
        yaml.dump(yaml_config, yaml_file)
    run_args = {
        "run_dir": rundir,
        "logdir": f"{rundir}/logs",
        "config_yml": config_yaml,
    }
    if update_run_args_with is not None:
        run_args.update(update_run_args_with)

    # run
    parser = flags.get_parser()
    args, override_args = parser.parse_known_args(
        ["--mode", "train", "--seed", "100", "--config-yml", "config.yml", "--cpu"]
    )
    for arg_name, arg_value in run_args.items():
        setattr(args, arg_name, arg_value)
    config = build_config(args, override_args)

    if world_size > 0:
        pg_config = PGConfig(
            backend="gloo", world_size=world_size, gp_group_size=1, use_gp=False
        )
        spawn_multi_process(
            pg_config,
            Runner(distributed=True),
            init_env_rank_and_launch_test,
            config,
        )
    else:
        Runner()(config)

    if save_checkpoint_to is not None:
        checkpoints = glob.glob(f"{rundir}/checkpoints/*/checkpoint.pt")
        assert len(checkpoints) == 1
        os.rename(checkpoints[0], save_checkpoint_to)
    if save_predictions_to is not None:
        predictions_filenames = glob.glob(f"{rundir}/results/*/s2ef_predictions.npz")
        assert len(predictions_filenames) == 1
        os.rename(predictions_filenames[0], save_predictions_to)
    return get_tensorboard_log_values(
        f"{rundir}/logs",
    )
