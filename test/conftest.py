import logging
import sys
from pathlib import Path

import pytest

sys.path.append("../scripts")
import util  # noqa: E402

from deploy import Cluster, Configuration  # noqa: E402

logging.basicConfig(level=logging.INFO)
log = logging.getLogger()

root = Path(__file__).parent.parent
tf_path = root / "terraform"
test_path = root / "test"


def pytest_addoption(parser):
    parser.addoption(
        "--project-id", action="store", help="GCP project to deploy the cluster to"
    )
    parser.addoption("--cluster-name", action="store", help="cluster name to deploy")
    none_list = set(
        [
            "null",
            "none",
        ]
    )
    parser.addoption(
        "--image",
        action="store",
        nargs="?",
        type=lambda a: None if a.lower() in none_list else a,
        help="image name to use for test cluster",
    )
    parser.addoption(
        "--image-family",
        action="store",
        nargs="?",
        help="image family to use for test cluster",
    )
    parser.addoption(
        "--image-project", action="store", help="image project to use for test cluster"
    )


@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(item, call):
    # execute all other hooks to obtain the report object
    outcome = yield
    rep = outcome.get_result()

    # set a report attribute for each phase of a call, which can
    # be "setup", "call", "teardown"

    setattr(item, "rep_" + rep.when, rep)


# We need to discriminate between arch (arm64, x86_64) because a configuration
# is mapped to a tfvars file that contains instances for an input image.
# Image arch must match instance arch, otherwise instance boot failure.
#
# NOTE: config key should follow: pytest.param("${ARCH}-${TF_CONFIG}")
#
# Use the following to discriminate arch for testing.
# $ pytest -k EXPR
CONFIGS = [
    dict(
        marks=("x86_64", "basic"),
        moduledir=tf_path / "slurm_cluster/examples/slurm_cluster/cloud/basic",
        tfvars_file=test_path / "x86_64-basic.tfvars.tpl",
        tfvars={},
    ),
    dict(
        marks=("arm64", "basic"),
        moduledir=tf_path / "slurm_cluster/examples/slurm_cluster/cloud/basic",
        tfvars_file=test_path / "arm64-basic.tfvars.tpl",
        tfvars={},
    ),
]
CONFIGS = {"-".join(conf["marks"]): conf for conf in CONFIGS}

params = (
    pytest.param(k, marks=[getattr(pytest.mark, mark) for mark in conf["marks"]])
    for k, conf in CONFIGS.items()
)


@pytest.fixture(params=params, scope="session")
def configuration(request):
    """fixture providing terraform cluster configuration"""
    project_id = request.config.getoption("project_id")
    cluster_name = request.config.getoption("cluster_name")
    image_project = request.config.getoption("image_project")
    image_family = request.config.getoption("image_family")
    image = request.config.getoption("image")

    config = Configuration(
        cluster_name=cluster_name,
        project_id=project_id,
        image_project=image_project,
        image_family=image_family,
        image=image,
        **CONFIGS[request.param],
    )
    log.info(f"init cluster {str(config)}")
    config.setup()
    return config


@pytest.fixture(scope="session")
def plan(configuration):
    return configuration.tf.plan(
        tf_vars=configuration.tfvars,
        tf_var_file=configuration.tfvars_file.name,
        output=True,
    )


@pytest.fixture(scope="session")
def applied(request, configuration):
    """fixture providing applied terraform handle"""
    request.addfinalizer(configuration.destroy)
    log.info(f"apply deployment {str(configuration)}")
    configuration.apply()
    return configuration.tf


@pytest.fixture(scope="session", autouse=True)
def cluster(request, applied):
    """fixture providing deploy.Cluster communication handle for the cluster"""
    cluster = Cluster(applied)

    def disconnect():
        nonlocal cluster
        cluster.save_logs()
        log.info("tearing down cluster")
        cluster.disconnect()
        # TODO verify all instances are removed

    request.addfinalizer(disconnect)
    log.info("waiting for cluster to be available")
    cluster.activate()
    log.info("cluster is now responding")
    return cluster


@pytest.fixture(scope="session")
def cfg(cluster: Cluster):
    """fixture providing util config for the cluster"""
    # download the config.yaml from the controller and load it locally
    cluster_name = cluster.tf.output()["slurm_cluster_name"]
    cfgfile = Path(f"{cluster_name}-config.yaml")
    cfgfile.write_text(
        cluster.controller_exec_output("sudo cat /slurm/scripts/config.yaml")
    )
    return util.load_config_file(cfgfile)


@pytest.fixture(scope="session")
def lkp(cfg: util.NSDict):
    """fixture providing util.Lookup for the cluster"""
    return util.Lookup(cfg)
