#!/usr/bin/env python3

# Copyright (C) SchedMD LLC.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import argparse
import collections
import importlib.util
import inspect
import json
import logging
import logging.config
import math
import os
import re
import shelve
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
from collections import defaultdict, namedtuple
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from functools import lru_cache, reduce, partialmethod
from itertools import chain, compress, islice
from pathlib import Path
from time import sleep, time

import slurm_gcp_plugins

required_modules = [
    ("googleapiclient", "google-api-python-client"),
    ("requests", "requests"),
    ("yaml", "yaml"),
    ("addict", "addict"),
    ("httplib2", "httplib2"),
    ("google.cloud.tpu_v2", "google-cloud-tpu"),
]
missing_imports = False
can_tpu = True
for module, name in required_modules:
    if importlib.util.find_spec(module) is None:
        if module == "google.cloud.tpu_v2":
            can_tpu = False
            print(
                f"WARNING: Missing Python module '{module} (pip:{name})', TPU support will not work."
            )
        else:
            missing_imports = True
            print(f"ERROR: Missing Python module '{module} (pip:{name})'")
if missing_imports:
    print("Aborting due to missing Python modules")
    exit(1)

import google.auth  # noqa: E402
from google.oauth2 import service_account  # noqa: E402
import googleapiclient.discovery  # noqa: E402
import google_auth_httplib2  # noqa: E402
from googleapiclient.http import set_user_agent  # noqa: E402
import httplib2  # noqa: E402

if can_tpu:
    from google.cloud import tpu_v2 as tpu  # noqa: E402
import google.api_core.exceptions as gExceptions  # noqa: E402

from requests import get as get_url  # noqa: E402
from requests.exceptions import RequestException  # noqa: E402

import yaml  # noqa: E402
from addict import Dict as NSDict  # noqa: E402

optional_modules = [
    ("google.cloud.secretmanager", "google-cloud-secret-manager"),
]
for module, name in optional_modules:
    if importlib.util.find_spec(module) is None:
        print(f"WARNING: Missing Python module '{module}' (pip:{name}) ")

USER_AGENT = "Slurm_GCP_Scripts/1.5 (GPN:SchedMD)"
ENV_CONFIG_YAML = os.getenv("SLURM_CONFIG_YAML")
if ENV_CONFIG_YAML:
    CONFIG_FILE = Path(ENV_CONFIG_YAML)
else:
    CONFIG_FILE = Path(__file__).with_name("config.yaml")
API_REQ_LIMIT = 2000
URI_REGEX = r"[a-z]([-a-z0-9]*[a-z0-9])?"

def_creds, auth_project = google.auth.default()
Path.mkdirp = partialmethod(Path.mkdir, parents=True, exist_ok=True)

scripts_dir = next(
    p for p in (Path(__file__).parent, Path("/slurm/scripts")) if p.is_dir()
)

# readily available compute api handle
compute = None
# slurm-gcp config object, could be empty if not available
cfg = NSDict()
# caching Lookup object
lkp = None

# load all directories as Paths into a dict-like namespace
dirs = NSDict(
    {
        n: Path(p)
        for n, p in dict.items(
            {
                "home": "/home",
                "apps": "/opt/apps",
                "slurm": "/slurm",
                "scripts": scripts_dir,
                "custom_scripts": "/slurm/custom_scripts",
                "munge": "/etc/munge",
                "secdisk": "/mnt/disks/sec",
                "log": "/var/log/slurm",
            }
        )
    }
)

slurmdirs = NSDict(
    {
        n: Path(p)
        for n, p in dict.items(
            {
                "prefix": "/usr/local",
                "etc": "/usr/local/etc/slurm",
                "state": "/var/spool/slurm",
            }
        )
    }
)


yaml.SafeDumper.yaml_representers[
    None
] = lambda self, data: yaml.representer.SafeRepresenter.represent_str(self, str(data))


class LogFormatter(logging.Formatter):
    """adds logging flags to the levelname in log records"""

    def format(self, record):
        new_fmt = self._fmt
        flag = getattr(record, "flag", None)
        if flag is not None:
            start, level, end = new_fmt.partition("%(levelname)s")
            if level:
                new_fmt = f"{start}{level}(%(flag)s){end}"
        # insert function name if record level is DEBUG
        if record.levelno < logging.INFO:
            prefix, msg, suffix = new_fmt.partition("%(message)s")
            new_fmt = f"{prefix}%(funcName)s: {msg}{suffix}"
        self._style._fmt = new_fmt
        return super().format(record)


class FlagLogAdapter(logging.LoggerAdapter):
    """creates log adapters that add a flag to the log record,
    allowing it to be filtered"""

    def __init__(self, logger, flag, extra=None):
        if extra is None:
            extra = {}
        self.flag = flag
        super().__init__(logger, extra)

    @property
    def enabled(self):
        return cfg.extra_logging_flags.get(self.flag, False)

    def process(self, msg, kwargs):
        extra = kwargs.setdefault("extra", {})
        extra.update(self.extra)
        extra["flag"] = self.flag
        return msg, kwargs


logging.basicConfig(level=logging.INFO, stream=sys.stdout)
log = logging.getLogger(__name__)
logging_flags = [
    "trace_api",
    "subproc",
    "hostlists",
]
log_trace_api = FlagLogAdapter(log, "trace_api")
log_subproc = FlagLogAdapter(log, "subproc")
log_hostlists = FlagLogAdapter(log, "hostlists")


def access_secret_version(project_id, secret_id, version_id="latest"):
    """
    Access the payload for the given secret version if one exists. The version
    can be a version number as a string (e.g. "5") or an alias (e.g. "latest").
    """
    from google.cloud import secretmanager
    from google.api_core import exceptions

    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{project_id}/secrets/{secret_id}/versions/{version_id}"
    try:
        response = client.access_secret_version(request={"name": name})
        log.debug(f"Secret '{name}' was found.")
        payload = response.payload.data.decode("UTF-8")
    except exceptions.NotFound:
        log.debug(f"Secret '{name}' was not found!")
        payload = None

    return payload


def parse_self_link(self_link: str):
    """Parse a selfLink url, extracting all useful values
    https://.../v1/projects/<project>/regions/<region>/...
    {'project': <project>, 'region': <region>, ...}
    can also extract zone, instance (name), image, etc
    """
    link_patt = re.compile(r"(?P<key>[^\/\s]+)s\/(?P<value>[^\s\/]+)")
    return NSDict(link_patt.findall(self_link))


def parse_bucket_uri(uri: str):
    """
    Parse a bucket url
    E.g. gs://<bucket_name>/<path>
    """
    pattern = re.compile(r"gs://(?P<bucket>[^/\s]+)/(?P<path>([^/\s]+)(/[^/\s]+)*)")
    matches = pattern.match(uri)
    return matches.group("bucket"), matches.group("path")


def trim_self_link(link: str):
    """get resource name from self link url, eg.
    https://.../v1/projects/<project>/regions/<region>
    -> <region>
    """
    try:
        return link[link.rindex("/") + 1 :]
    except ValueError:
        raise Exception(f"'/' not found, not a self link: '{link}' ")


def execute_with_futures(func, seq):
    with ThreadPoolExecutor() as exe:
        futures = []
        for i in seq:
            future = exe.submit(func, i)
            futures.append(future)
        for future in as_completed(futures):
            result = future.exception()
            if result is not None:
                raise result


def map_with_futures(func, seq):
    with ThreadPoolExecutor() as exe:
        futures = []
        for i in seq:
            future = exe.submit(func, i)
            futures.append(future)
        for future in futures:
            # Will be result or raise Exception
            res = None
            try:
                res = future.result()
            except Exception as e:
                res = e
            yield res


def blob_get(file, project=None):
    from google.cloud import storage

    if project is None:
        project = lkp.project
    uri = instance_metadata("attributes/slurm_bucket_path")
    bucket_name, path = parse_bucket_uri(uri)
    blob_name = f"{path}/{file}"
    storage_client = storage.Client(project=project)
    return storage_client.get_bucket(bucket_name).blob(blob_name)


def blob_list(prefix="", delimiter=None, project=None):
    from google.cloud import storage

    if project is None:
        project = lkp.project
    uri = instance_metadata("attributes/slurm_bucket_path")
    bucket_name, path = parse_bucket_uri(uri)
    blob_prefix = f"{path}/{prefix}"
    storage_client = storage.Client(project=project)
    # Note: The call returns a response only when the iterator is consumed.
    blobs = storage_client.list_blobs(
        bucket_name, prefix=blob_prefix, delimiter=delimiter
    )
    return [blob for blob in blobs]


def reservation_resource_policies(reservation):
    """
    Inspects reservation object, returns list of resource policies names.
    Converts policy URLs to names, e.g.:
    projects/111111/regions/us-central1/resourcePolicies/zebra -> zebra
    """
    return [u.split("/")[-1] for u in reservation.get("resourcePolicies", {}).values()]


def compute_service(credentials=None, user_agent=USER_AGENT, version="v1"):
    """Make thread-safe compute service handle
    creates a new Http for each request
    """
    try:
        key_path = os.environ["GOOGLE_APPLICATION_CREDENTIALS"]
    except KeyError:
        key_path = None
    if key_path is not None:
        credentials = service_account.Credentials.from_service_account_file(
            key_path, scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
    elif credentials is None:
        credentials = def_creds

    def build_request(http, *args, **kwargs):
        new_http = httplib2.Http()
        if user_agent is not None:
            new_http = set_user_agent(new_http, user_agent)
        if credentials is not None:
            new_http = google_auth_httplib2.AuthorizedHttp(credentials, http=new_http)
        return googleapiclient.http.HttpRequest(new_http, *args, **kwargs)

    log.debug(f"Using version={version} of Google Compute Engine API")
    return googleapiclient.discovery.build(
        "compute",
        version,
        requestBuilder=build_request,
        credentials=credentials,
    )


compute = compute_service()


def load_config_data(config):
    """load dict-like data into a config object"""
    cfg = NSDict(config)
    if not cfg.slurm_log_dir:
        cfg.slurm_log_dir = dirs.log
    if not cfg.slurm_bin_dir:
        cfg.slurm_bin_dir = slurmdirs.prefix / "bin"
    if not cfg.slurm_control_host:
        cfg.slurm_control_host = f"{cfg.slurm_cluster_name}-controller"
    if not cfg.slurm_control_host_port:
        cfg.slurm_control_host_port = "6820-6830"
    if not cfg.munge_mount:
        # NOTE: should only happen with cloud controller
        cfg.munge_mount = NSDict(
            {
                "server_ip": cfg.slurm_control_addr or cfg.slurm_control_host,
                "remote_mount": "/etc/munge",
                "fs_type": "nfs",
                "mount_options": "defaults,hard,intr,_netdev",
            }
        )

    if not cfg.enable_debug_logging and isinstance(cfg.enable_debug_logging, NSDict):
        cfg.enable_debug_logging = False
    cfg.extra_logging_flags = NSDict(
        {flag: cfg.extra_logging_flags.get(flag, False) for flag in logging_flags}
    )
    return cfg


def new_config(config):
    """initialize a new config object
    necessary defaults are handled here
    """
    cfg = load_config_data(config)

    network_storage_iter = filter(
        None,
        (
            *cfg.network_storage,
            *cfg.login_network_storage,
            *chain.from_iterable(p.network_storage for p in cfg.partitions.values()),
        ),
    )
    for netstore in network_storage_iter:
        if netstore != "gcsfuse" and (
            netstore.server_ip is None or netstore.server_ip == "$controller"
        ):
            netstore.server_ip = cfg.slurm_control_host
    return cfg


def fetch_config_yaml():
    """Fetch config.yaml from bucket"""
    config_yaml = blob_get("config.yaml").download_as_text()
    cfg = new_config(yaml.safe_load(config_yaml))
    return cfg


def fetch_config_yaml_md5():
    """Fetch config.yaml blob md5 from bucket"""
    import hashlib

    blob = blob_get("config.yaml")
    blob.reload()  # Populate blob with metadata
    hash_str = str(blob.md5_hash).encode(encoding="utf-8")
    return hashlib.md5(hash_str)


def load_config_file(path):
    """load config from file"""
    content = None
    try:
        content = yaml.safe_load(Path(path).read_text())
    except FileNotFoundError:
        log.warning(f"config file not found: {path}")
        return NSDict()
    return load_config_data(content)


def save_config(cfg, path):
    """save given config to file at path"""
    Path(path).write_text(yaml.dump(cfg, Dumper=Dumper))


def filter_logging_flags(record):
    """logging filter for flags
    if there are no flags, always pass. If there are flags, only pass if a flag
    matches an enabled flag in cfg.extra_logging_flags"""
    flag = getattr(record, "flag", None)
    if flag is None:
        return True
    return cfg.extra_logging_flags.get(flag, False)


def owned_file_handler(filename):
    """create file handler"""
    if filename is None:
        return None
    chown_slurm(filename)
    return logging.handlers.WatchedFileHandler(filename, delay=True)


def config_root_logger(caller_logger, level="DEBUG", stdout=True, logfile=None):
    """configure the root logger, disabling all existing loggers"""
    handlers = list(compress(("stdout_handler", "file_handler"), (stdout, logfile)))

    config = {
        "version": 1,
        "disable_existing_loggers": True,
        "formatters": {
            "standard": {
                "()": LogFormatter,
                "fmt": "%(levelname)s: %(message)s",
            },
            "stamp": {
                "()": LogFormatter,
                "fmt": "%(asctime)s %(levelname)s: %(message)s",
            },
        },
        "filters": {
            "logging_flags": {"()": lambda: filter_logging_flags},
        },
        "handlers": {
            "stdout_handler": {
                "level": logging.DEBUG,
                "formatter": "standard",
                "class": "logging.StreamHandler",
                "stream": sys.stdout,
                "filters": ["logging_flags"],
            },
            "file_handler": {
                "()": owned_file_handler,
                "level": logging.DEBUG,
                "formatter": "stamp",
                "filters": ["logging_flags"],
                "filename": logfile,
            },
        },
        "root": {
            "handlers": handlers,
            "level": level,
        },
    }
    if not logfile:
        del config["handlers"]["file_handler"]
    logging.config.dictConfig(config)
    loggers = (
        __name__,
        "resume",
        "suspend",
        "slurmsync",
        "setup",
        caller_logger,
    )
    for logger in map(logging.getLogger, loggers):
        logger.disabled = False


def log_api_request(request):
    """log.trace info about a compute API request"""
    if log_trace_api.enabled:
        # output the whole request object as pretty yaml
        # the body is nested json, so load it as well
        rep = json.loads(request.to_json())
        if rep.get("body", None) is not None:
            rep["body"] = json.loads(rep["body"])
        pretty_req = yaml.safe_dump(rep).rstrip()
        # label log message with the calling function
        log_trace_api.debug(f"{inspect.stack()[1].function}:\n{pretty_req}")


def handle_exception(exc_type, exc_value, exc_trace):
    """log exceptions other than KeyboardInterrupt"""
    # TODO does this work?
    if not issubclass(exc_type, KeyboardInterrupt):
        log.exception("Fatal exception", exc_info=(exc_type, exc_value, exc_trace))
    sys.__excepthook__(exc_type, exc_value, exc_trace)


def run(
    args,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    shell=False,
    timeout=None,
    check=True,
    universal_newlines=True,
    **kwargs,
):
    """Wrapper for subprocess.run() with convenient defaults"""
    if isinstance(args, list):
        args = list(filter(lambda x: x is not None, args))
        args = " ".join(args)
    if not shell and isinstance(args, str):
        args = shlex.split(args)
    log_subproc.debug(f"run: {args}")
    result = subprocess.run(
        args,
        stdout=stdout,
        stderr=stderr,
        shell=shell,
        timeout=timeout,
        check=check,
        universal_newlines=universal_newlines,
        **kwargs,
    )
    return result


def spawn(cmd, quiet=False, shell=False, **kwargs):
    """nonblocking spawn of subprocess"""
    if not quiet:
        log_subproc.debug(f"spawn: {cmd}")
    args = cmd if shell else shlex.split(cmd)
    return subprocess.Popen(args, shell=shell, **kwargs)


def chown_slurm(path, mode=None):
    if path.exists():
        if mode:
            path.chmod(mode)
    else:
        path.parent.mkdirp()
        if mode:
            path.touch(mode=mode)
        else:
            path.touch()
    try:
        shutil.chown(path, user="slurm", group="slurm")
    except LookupError:
        log.warning(f"User 'slurm' does not exist. Cannot 'chown slurm:slurm {path}'.")
    except PermissionError:
        log.warning(f"Not authorized to 'chown slurm:slurm {path}'.")
    except Exception as err:
        log.error(err)


@contextmanager
def cd(path):
    """Change working directory for context"""
    prev = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(prev)


def with_static(**kwargs):
    def decorate(func):
        for var, val in kwargs.items():
            setattr(func, var, val)
        return func

    return decorate


def cached_property(f):
    return property(lru_cache()(f))


def separate(pred, coll):
    """filter into 2 lists based on pred returning True or False
    returns ([False], [True])
    """
    return reduce(lambda acc, el: acc[pred(el)].append(el) or acc, coll, ([], []))


def chunked(iterable, n=API_REQ_LIMIT):
    """group iterator into chunks of max size n"""
    it = iter(iterable)
    while True:
        chunk = list(islice(it, n))
        if not chunk:
            return
        yield chunk


def groupby_unsorted(seq, key):
    indices = defaultdict(list)
    for i, el in enumerate(seq):
        indices[key(el)].append(i)
    for k, idxs in indices.items():
        yield k, (seq[i] for i in idxs)


@lru_cache(maxsize=32)
def find_ratio(a, n, s, r0=None):
    """given the start (a), count (n), and sum (s), find the ratio required"""
    if n == 2:
        return s / a - 1
    an = a * n
    if n == 1 or s == an:
        return 1
    if r0 is None:
        # we only need to know which side of 1 to guess, and the iteration will work
        r0 = 1.1 if an < s else 0.9

    # geometric sum formula
    def f(r):
        return a * (1 - r**n) / (1 - r) - s

    # derivative of f
    def df(r):
        rm1 = r - 1
        rn = r**n
        return (a * (rn * (n * rm1 - r) + r)) / (r * rm1**2)

    MIN_DR = 0.0001  # negligible change
    r = r0
    # print(f"r(0)={r0}")
    MAX_TRIES = 64
    for i in range(1, MAX_TRIES + 1):
        try:
            dr = f(r) / df(r)
        except ZeroDivisionError:
            log.error(f"Failed to find ratio due to zero division! Returning r={r0}")
            return r0
        r = r - dr
        # print(f"r({i})={r}")
        # if the change in r is small, we are close enough
        if abs(dr) < MIN_DR:
            break
    else:
        log.error(f"Could not find ratio after {MAX_TRIES}! Returning r={r0}")
        return r0
    return r


def backoff_delay(start, timeout=None, ratio=None, count: int = 0):
    """generates `count` waits starting at `start`
    sum of waits is `timeout` or each one is `ratio` bigger than the last
    the last wait is always 0"""
    # timeout or ratio must be set but not both
    assert (timeout is None) ^ (ratio is None)
    assert ratio is None or ratio > 0
    assert timeout is None or timeout >= start
    assert (count > 1 or timeout is not None) and isinstance(count, int)
    assert start > 0

    if count == 0:
        # Equation for auto-count is tuned to have a max of
        # ~int(timeout) counts with a start wait of <0.01.
        # Increasing start wait decreases count eg.
        # backoff_delay(10, timeout=60) -> count = 5
        count = int(
            (timeout / ((start + 0.05) ** (1 / 2)) + 2) // math.log(timeout + 2)
        )

    yield start
    # if ratio is set:
    # timeout = start * (1 - ratio**(count - 1)) / (1 - ratio)
    if ratio is None:
        ratio = find_ratio(start, count - 1, timeout)

    wait = start
    # we have start and 0, so we only need to generate count - 2
    for _ in range(count - 2):
        wait *= ratio
        yield wait
    yield 0
    return


ROOT_URL = "http://metadata.google.internal/computeMetadata/v1"


def get_metadata(path, root=ROOT_URL):
    """Get metadata relative to metadata/computeMetadata/v1"""
    HEADERS = {"Metadata-Flavor": "Google"}
    url = f"{root}/{path}"
    try:
        resp = get_url(url, headers=HEADERS)
        resp.raise_for_status()
        return resp.text
    except RequestException:
        log.debug(f"metadata not found ({url})")
        raise Exception(f"failed to get_metadata from {url}")


@lru_cache(maxsize=None)
def instance_metadata(path):
    """Get instance metadata"""
    return get_metadata(path, root=f"{ROOT_URL}/instance")


@lru_cache(maxsize=None)
def project_metadata(key):
    """Get project metadata project/attributes/<slurm_cluster_name>-<path>"""
    return get_metadata(key, root=f"{ROOT_URL}/project/attributes")


def bucket_blob_download(bucket_name, blob_name):
    from google.cloud import storage

    storage_client = storage.Client()
    bucket = storage_client.bucket(bucket_name)
    blob = bucket.blob(blob_name)
    contents = None
    with tempfile.NamedTemporaryFile(mode="w+t") as tmp:
        blob.download_to_filename(tmp.name)
        with open(tmp.name, "r") as f:
            contents = f.read()
    return contents


def natural_sort(text):
    def atoi(text):
        return int(text) if text.isdigit() else text

    return [atoi(w) for w in re.split(r"(\d+)", text)]


def to_hostlist(nodenames):
    """make hostlist from list of node names"""
    # use tmp file because list could be large
    tmp_file = tempfile.NamedTemporaryFile(mode="w+t", delete=False)
    tmp_file.writelines("\n".join(sorted(nodenames, key=natural_sort)))
    tmp_file.close()

    hostlist = run(f"{lkp.scontrol} show hostlist {tmp_file.name}").stdout.rstrip()
    log_hostlists.debug(f"hostlist({len(nodenames)}): {hostlist}".format(hostlist))
    os.remove(tmp_file.name)
    return hostlist


def part_is_tpu(part):
    """check if partition with name part contains a nodeset of type tpu"""
    return len(lkp.cfg.partitions[part].partition_nodeset_tpu) > 0


def get_vmcount_of_tpu_part(part):
    res = 0
    for ns in lkp.cfg.partitions[part].partition_nodeset_tpu:
        tpu_obj = TPU(lkp.cfg.nodeset_tpu[ns])
        if res == 0:
            res = tpu_obj.vmcount
        else:
            if res != tpu_obj.vmcount:
                # this should not happen, that in the same partition there are different vmcount nodesets
                return -1
    return res


def to_hostnames(nodelist):
    """make list of hostnames from hostlist expression"""
    if isinstance(nodelist, str):
        hostlist = nodelist
    else:
        hostlist = ",".join(nodelist)
    hostnames = run(f"{lkp.scontrol} show hostnames {hostlist}").stdout.splitlines()
    log_hostlists.debug(f"hostnames({len(hostnames)}) from {hostlist}")
    return hostnames


def retry_exception(exc):
    """return true for exceptions that should always be retried"""
    retry_errors = (
        "Rate Limit Exceeded",
        "Quota Exceeded",
    )
    return any(e in str(exc) for e in retry_errors)


def ensure_execute(request):
    """Handle rate limits and socket time outs"""

    for retry, wait in enumerate(backoff_delay(0.5, timeout=10 * 60, count=20)):
        try:
            return request.execute()
        except googleapiclient.errors.HttpError as e:
            if retry_exception(e):
                log.error(f"retry:{retry} '{e}'")
                sleep(wait)
                continue
            raise

        except socket.timeout as e:
            # socket timed out, try again
            log.debug(e)

        except Exception as e:
            log.error(e, exc_info=True)
            raise

        break


def batch_execute(requests, compute=compute, retry_cb=None):
    """execute list or dict<req_id, request> as batch requests
    retry if retry_cb returns true
    """
    BATCH_LIMIT = 1000
    if not isinstance(requests, dict):
        requests = {str(k): v for k, v in enumerate(requests)}  # rid generated here
    done = {}
    failed = {}
    timestamps = []
    rate_limited = False

    def batch_callback(rid, resp, exc):
        nonlocal rate_limited
        if exc is not None:
            log.error(f"compute request exception {rid}: {exc}")
            if retry_exception(exc):
                rate_limited = True
            else:
                req = requests.pop(rid)
                failed[rid] = (req, exc)
        else:
            # if retry_cb is set, don't move to done until it returns false
            if retry_cb is None or not retry_cb(resp):
                requests.pop(rid)
                done[rid] = resp

    def batch_request(reqs):
        batch = compute.new_batch_http_request(callback=batch_callback)
        for rid, req in reqs:
            batch.add(req, request_id=rid)
        return batch

    while requests:
        if timestamps:
            timestamps = [stamp for stamp in timestamps if stamp > time()]
        if rate_limited and timestamps:
            stamp = next(iter(timestamps))
            sleep(max(stamp - time(), 0))
            rate_limited = False
        # up to API_REQ_LIMIT (2000) requests
        # in chunks of up to BATCH_LIMIT (1000)
        batches = [
            batch_request(chunk)
            for chunk in chunked(islice(requests.items(), API_REQ_LIMIT), BATCH_LIMIT)
        ]
        timestamps.append(time() + 100)
        with ThreadPoolExecutor() as exe:
            futures = []
            for batch in batches:
                future = exe.submit(ensure_execute, batch)
                futures.append(future)
            for future in futures:
                result = future.exception()
                if result is not None:
                    raise result

    return done, failed


def wait_request(operation, project=None, compute=compute):
    """makes the appropriate wait request for a given operation"""
    if project is None:
        project = lkp.project
    if "zone" in operation:
        req = compute.zoneOperations().wait(
            project=project,
            zone=trim_self_link(operation["zone"]),
            operation=operation["name"],
        )
    elif "region" in operation:
        req = compute.regionOperations().wait(
            project=project,
            region=trim_self_link(operation["region"]),
            operation=operation["name"],
        )
    else:
        req = compute.globalOperations().wait(
            project=project, operation=operation["name"]
        )
    return req


def wait_for_operation(operation, project=None, compute=compute):
    """wait for given operation"""
    if project is None:
        project = parse_self_link(operation["selfLink"]).project
    wait_req = wait_request(operation, project=project, compute=compute)

    while True:
        result = ensure_execute(wait_req)
        if result["status"] == "DONE":
            log_errors = " with errors" if "error" in result else ""
            log.debug(
                f"operation complete{log_errors}: type={result['operationType']}, name={result['name']}"
            )
            return result


def wait_for_operations(operations, project=None, compute=compute):
    return [
        wait_for_operation(op, project=project, compute=compute) for op in operations
    ]


def wait_for_operations_async(operations, project=None, compute=compute):
    """wait for all operations"""

    def operation_retry(resp):
        return resp["status"] != "DONE"

    requests = [wait_request(op, project=project, compute=compute) for op in operations]
    return batch_execute(requests, retry_cb=operation_retry)


def get_filtered_operations(
    op_filter,
    zone=None,
    region=None,
    only_global=False,
    project=None,
    compute=compute,
):
    """get list of operations associated with group id"""

    if project is None:
        project = lkp.project
    operations = []

    def get_aggregated_operations(items):
        # items is a dict of location key to value: dict(operations=<list of operations>) or an empty dict
        operations.extend(
            chain.from_iterable(
                ops["operations"] for ops in items.values() if "operations" in ops
            )
        )

    def get_list_operations(items):
        operations.extend(items)

    handle_items = get_list_operations
    if only_global:
        act = compute.globalOperations()
        op = act.list(project=project, filter=op_filter)
        nxt = act.list_next
    elif zone is not None:
        act = compute.zoneOperations()
        op = act.list(project=project, zone=zone, filter=op_filter)
        nxt = act.list_next
    elif region is not None:
        act = compute.regionOperations()
        op = act.list(project=project, region=region, filter=op_filter)
        nxt = act.list_next
    else:
        act = compute.globalOperations()
        op = act.aggregatedList(
            project=project, filter=op_filter, fields="items.*.operations,nextPageToken"
        )
        nxt = act.aggregatedList_next
        handle_items = get_aggregated_operations
    while op is not None:
        result = ensure_execute(op)
        handle_items(result["items"])
        op = nxt(op, result)
    return operations


def get_insert_operations(group_ids, flt=None, project=None, compute=compute):
    """get all insert operations from a list of operationGroupId"""
    if project is None:
        project = lkp.project
    if isinstance(group_ids, str):
        group_ids = group_ids.split(",")
    filters = [
        "operationType=insert",
        flt,
        " OR ".join(f"(operationGroupId={id})" for id in group_ids),
    ]
    return get_filtered_operations(" AND ".join(f"({f})" for f in filters if f))


def machine_type_sockets(template):
    pattern = re.compile("^(?P<family>[^-]+)")
    m = pattern.match(template.machineType)
    if not m:
        raise Exception(f"template {template} does not match expected regex")
    family = m.group("family")
    guestCpus: int = int(template.machine_info.guestCpus)
    socket_count = dict.get(
        {
            "h3": 2,
            "c2d": 2 if guestCpus > 56 else 1,
        },
        family,
        1,  # assume 1 socket for all other families
    )
    return socket_count


def isSmt(template):
    machineType: str = template.machineType
    guestCpus: int = int(template.machine_info.guestCpus)

    pattern = re.compile("^(?P<family>[^-]+)")
    matches = pattern.match(machineType)
    machineTypeFamily: str = matches["family"]

    # https://cloud.google.com/compute/docs/cpu-platforms
    noSmtFamily = [
        "t2a",
        "t2d",
        "h3",
    ]
    if machineTypeFamily in noSmtFamily:
        return False
    elif guestCpus == 1:
        return False
    return True


def getThreadsPerCore(template):
    threadsPerCore: int = template.advancedMachineFeatures.threadsPerCore

    if not isSmt(template):
        return 1
    elif threadsPerCore:
        return threadsPerCore
    else:
        return 2


class Dumper(yaml.SafeDumper):
    """Add representers for pathlib.Path and NSDict for yaml serialization"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.add_representer(NSDict, self.represent_nsdict)
        self.add_multi_representer(Path, self.represent_path)

    @staticmethod
    def represent_nsdict(dumper, data):
        return dumper.represent_mapping("tag:yaml.org,2002:map", data.items())

    @staticmethod
    def represent_path(dumper, path):
        return dumper.represent_scalar("tag:yaml.org,2002:str", str(path))


class TPU:
    """Class for handling the TPU-vm nodes"""

    if can_tpu:
        State = tpu.types.cloud_tpu.Node.State
        TPUS_PER_VM = 4
        __expected_states = {
            "create": State.READY,
            "start": State.READY,
            "stop": State.STOPPED,
        }

        __tpu_version_mapping = {
            "V2": tpu.AcceleratorConfig().Type.V2,
            "V3": tpu.AcceleratorConfig().Type.V3,
            "V4": tpu.AcceleratorConfig().Type.V4,
        }

    def __init__(self, nodeset):
        if not can_tpu:
            raise Exception("TPU pip package not installed")
        self._nodeset = nodeset
        self._parent = f"projects/{lkp.project}/locations/{nodeset.zone}"
        self._client = tpu.TpuClient()
        self.data_disks = []
        for data_disk in nodeset.data_disks:
            ad = tpu.AttachedDisk()
            ad.source_disk = data_disk
            ad.mode = tpu.AttachedDisk.DiskMode.DISK_MODE_UNSPECIFIED
            self.data_disks.append(ad)
        ns_ac = nodeset.accelerator_config
        if ns_ac.topology != "" and ns_ac.version != "":
            ac = tpu.AcceleratorConfig()
            ac.topology = ns_ac.topology
            ac.type_ = self.__tpu_version_mapping[ns_ac.version]
            self.ac = ac
        else:
            req = tpu.GetAcceleratorTypeRequest(
                name=f"{self._parent}/acceleratorTypes/{nodeset.node_type}"
            )
            self.ac = self._client.get_accelerator_type(req).accelerator_configs[0]
        self.vmcount = self.__calc_vm_from_topology(self.ac.topology)

    @property
    def nodeset(self):
        return self._nodeset

    @property
    def preserve_tpu(self):
        return self._nodeset.preserve_tpu

    @property
    def node_type(self):
        return self._nodeset.node_type

    @property
    def tf_version(self):
        return self._nodeset.tf_version

    @property
    def enable_public_ip(self):
        return self._nodeset.enable_public_ip

    @property
    def preemptible(self):
        return self._nodeset.preemptible

    @property
    def reserved(self):
        return self._nodeset.reserved

    @property
    def service_account(self):
        return self._nodeset.service_account

    @property
    def zone(self):
        return self._nodeset.zone

    def check_node_type(self):
        if self.node_type is None:
            return False
        try:
            request = tpu.GetAcceleratorTypeRequest(
                name=f"{self._parent}/acceleratorTypes/{self.node_type}"
            )
            return self._client.get_accelerator_type(request=request) is not None
        except Exception:
            return False

    def check_tf_version(self):
        try:
            request = tpu.GetRuntimeVersionRequest(
                name=f"{self._parent}/runtimeVersions/{self.tf_version}"
            )
            return self._client.get_runtime_version(request=request) is not None
        except Exception:
            return False

    def __calc_vm_from_topology(self, topology):
        topo = topology.split("x")
        tot = 1
        for num in topo:
            tot = tot * int(num)
        return tot // self.TPUS_PER_VM

    def __check_resp(self, response, op_name):
        des_state = self.__expected_states.get(op_name)
        # If the state is not in the table just print the response
        if des_state is None:
            return False
        if response.__class__.__name__ != "Node":  # If the response is not a node fail
            return False
        if response.state == des_state:
            return True
        return False

    def list_nodes(self):
        try:
            request = tpu.ListNodesRequest(parent=self._parent)
            res = self._client.list_nodes(request=request)
        except gExceptions.NotFound:
            res = None
        return res

    def list_node_names(self):
        return [node.name.split("/")[-1] for node in self.list_nodes()]

    def start_node(self, nodename):
        request = tpu.StartNodeRequest(name=f"{self._parent}/nodes/{nodename}")
        resp = self._client.start_node(request=request).result()
        return self.__check_resp(resp, "start")

    def stop_node(self, nodename):
        request = tpu.StopNodeRequest(name=f"{self._parent}/nodes/{nodename}")
        resp = self._client.stop_node(request=request).result()
        return self.__check_resp(resp, "stop")

    def get_node(self, nodename):
        try:
            request = tpu.GetNodeRequest(name=f"{self._parent}/nodes/{nodename}")
            res = self._client.get_node(request=request)
        except gExceptions.NotFound:
            res = None
        return res

    def _register_node(self, nodename, ip_addr):
        dns_name = socket.getnameinfo((ip_addr, 0), 0)[0]
        run(
            f"{lkp.scontrol} update nodename={nodename} nodeaddr={ip_addr} nodehostname={dns_name}"
        )

    def create_node(self, nodename):
        if self.vmcount > 1 and not isinstance(nodename, list):
            log.error(
                f"Tried to create a {self.vmcount} node TPU on nodeset {self._nodeset.nodeset_name} but only received one nodename {nodename}"
            )
            return False
        if self.vmcount > 1 and (
            isinstance(nodename, list) and len(nodename) != self.vmcount
        ):
            log.error(
                f"Expected to receive a list of {self.vmcount} nodenames for TPU node creation in nodeset {self._nodeset.nodeset_name}, but received this list {nodename}"
            )
            return False

        node = tpu.Node()
        node.accelerator_config = self.ac
        node.runtime_version = f"tpu-vm-tf-{self.tf_version}"
        startup_script = """
        #!/bin/bash
        echo "startup script not found > /var/log/startup_error.log"
        """
        with open(
            Path(cfg.slurm_scripts_dir or dirs.scripts) / "startup.sh", "r"
        ) as script:
            startup_script = script.read()
        if isinstance(nodename, list):
            node_id = nodename[0]
            slurm_names = []
            wid = 0
            for node_wid in nodename:
                slurm_names.append(f"WORKER_{wid}:{node_wid}")
                wid += 1
        else:
            node_id = nodename
            slurm_names = [f"WORKER_0:{nodename}"]
        node.metadata = {
            "slurm_docker_image": self.nodeset.docker_image,
            "startup-script": startup_script,
            "slurm_instance_role": "compute",
            "slurm_cluster_name": lkp.cfg.slurm_cluster_name,
            "slurm_bucket_path": lkp.cfg.bucket_path,
            "slurm_names": ";".join(slurm_names),
        }
        node.tags = [lkp.cfg.slurm_cluster_name]
        if self.nodeset.service_account:
            node.service_account.email = self.nodeset.service_account.email
            node.service_account.scope = self.nodeset.service_account.scopes
        node.scheduling_config.preemptible = self.preemptible
        node.scheduling_config.reserved = self.reserved
        if self.nodeset.network:
            node.network_config.network = self.nodeset.network
        if self.nodeset.subnetwork:
            node.network_config.subnetwork = self.nodeset.subnetwork
        node.network_config.enable_external_ips = self.enable_public_ip
        if self.data_disks:
            node.data_disks = self.data_disks

        request = tpu.CreateNodeRequest(parent=self._parent, node=node, node_id=node_id)
        resp = self._client.create_node(request=request).result()
        if not self.__check_resp(resp, "create"):
            return False
        if isinstance(nodename, list):
            for node_id, net_endpoint in zip(nodename, resp.network_endpoints):
                self._register_node(node_id, net_endpoint.ip_address)
        else:
            ip_add = resp.network_endpoints[0].ip_address
            self._register_node(nodename, ip_add)
        return True

    def delete_node(self, nodename):
        request = tpu.DeleteNodeRequest(name=f"{self._parent}/nodes/{nodename}")
        try:
            resp = self._client.delete_node(request=request).result()
            if resp:
                return self.get_node(nodename=nodename) is None
            return False
        except gExceptions.NotFound:
            # log only error if vmcount is 1 as for other tpu vm count, this could be "phantom" nodes
            if self.vmcount == 1:
                log.error(f"Tpu single node {nodename} not found")
            else:
                # for the TPU nodes that consist in more than one vm, only the first node of the TPU a.k.a. the master node will
                # exist as real TPU nodes, so the other ones are expected to not be found, check the hostname of the node that has
                # not been found, and if it ends in 0, it means that is the master node and it should have been found, and in consequence
                # log an error
                nodehostname = yaml.safe_load(
                    run(f"{lkp.scontrol} --yaml show node {nodename}").stdout.rstrip()
                )["nodes"][0]["hostname"]
                if nodehostname.split("-")[-1] == "0":
                    log.error(f"TPU master node {nodename} not found")
                else:
                    log.info(f"Deleted TPU 'phantom' node {nodename}")
            # If the node is not found it is tecnichally deleted, so return success.
            return True


class Lookup:
    """Wrapper class for cached data access"""

    regex = (
        r"^(?P<prefix>"
        r"(?P<cluster>[^\s\-]+)"
        r"-(?P<nodeset>\S+)"
        r")"
        r"-(?P<node>"
        r"(?P<index>\d+)|"
        r"(?P<range>\[[\d,-]+\])"
        r")$"
    )
    node_desc_regex = re.compile(regex)

    def __init__(self, cfg=None):
        self._cfg = cfg or NSDict()
        self.template_cache_path = Path(__file__).parent / "template_info.cache"

    @property
    def cfg(self):
        return self._cfg

    @property
    def project(self):
        return self.cfg.project or auth_project

    @property
    def control_addr(self):
        return self.cfg.slurm_control_addr

    @property
    def control_host(self):
        return self.cfg.slurm_control_host

    @cached_property
    def control_host_addr(self):
        return socket.gethostbyname(self.cfg.slurm_control_host)

    @property
    def control_host_port(self):
        return self.cfg.slurm_control_host_port

    @property
    def scontrol(self):
        return Path(self.cfg.slurm_bin_dir if cfg else "") / "scontrol"

    @property
    def sinfo_bin(self):
        return Path(self.cfg.slurm_bin_dir if cfg else "") / "sinfo"

    def sinfo(self, cmd=None):
        cmd = " ".join(str(s) for s in (self.sinfo_bin, cmd) if s is not None)
        return run(cmd).stdout.rstrip()

    @property
    def squeue_bin(self):
        return Path(self.cfg.slurm_bin_dir if cfg else "") / "squeue"

    def squeue(self, cmd=None):
        cmd = " ".join(str(s) for s in (self.squeue_bin, cmd) if s is not None)
        return run(cmd).stdout.rstrip()

    @property
    def template_map(self):
        return self.cfg.template_map

    @cached_property
    def instance_role(self):
        return instance_metadata("attributes/slurm_instance_role")

    @cached_property
    def instance_role_safe(self):
        try:
            role = self.instance_role
        except Exception as e:
            log.error(e)
            role = None
        return role

    @cached_property
    def compute(self):
        # TODO evaluate when we need to use google_app_cred_path
        if self.cfg.google_app_cred_path:
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = self.cfg.google_app_cred_path
        return compute_service()

    @cached_property
    def hostname(self):
        return socket.gethostname()

    @cached_property
    def hostname_fqdn(self):
        return socket.getfqdn()

    @cached_property
    def zone(self):
        return instance_metadata("zone")

    @property
    def enable_job_exclusive(self):
        return bool(self.cfg.enable_job_exclusive or self.cfg.enable_placement)

    @lru_cache(maxsize=None)
    def _node_desc(self, node_name):
        """Get parts from node name"""
        if not node_name:
            node_name = self.hostname
        m = self.node_desc_regex.match(node_name)
        if not m:
            raise Exception(f"node name {node_name} is not valid")
        return NSDict(m.groupdict())

    def node_prefix(self, node_name=None):
        return self._node_desc(node_name).prefix

    def node_cluster_name(self, node_name=None):
        return self._node_desc(node_name).cluster

    def node_nodeset_name(self, node_name=None):
        return self._node_desc(node_name).nodeset

    def node_index(self, node_name=None):
        return int(self._node_desc(node_name).index)

    def node_nodeset(self, node_name=None):
        nodeset_name = self.node_nodeset_name(node_name)
        ns = self.cfg.nodeset.get(nodeset_name)
        if ns:
            return ns
        return self.cfg.nodeset_tpu.get(nodeset_name)

    def node_is_tpu(self, node_name=None):
        nodeset_name = self.node_nodeset_name(node_name)
        return self.cfg.nodeset_tpu.get(nodeset_name) is not None

    def chunk_tpu_nodes(self, tpu_nodes):
        model = tpu_nodes[0]
        tpu = TPU(self.node_nodeset(model))
        return chunked(tpu_nodes, n=tpu.vmcount)

    def node_template(self, node_name=None):
        return self.node_nodeset(node_name).instance_template

    def node_template_info(self, node_name=None):
        return self.template_info(self.node_template(node_name))

    def node_region(self, node_name=None):
        nodeset = self.node_nodeset(node_name)
        return parse_self_link(nodeset.subnetwork).region

    def node_is_static(self, node_name=None):
        nodeset = self.node_nodeset(node_name)
        return self.node_index(node_name) < nodeset.node_count_static

    def nodeset_prefix(self, nodeset_name):
        return f"{self.cfg.slurm_cluster_name}-{nodeset_name}"

    def nodeset_lists(self, nodeset):
        """Return static and dynamic nodenames given a partition node type
        definition
        """

        def node_range(count, start=0):
            end = start + count - 1
            return f"{start}" if count == 1 else f"[{start}-{end}]", end + 1

        prefix = self.nodeset_prefix(nodeset.nodeset_name)
        static_count = nodeset.node_count_static
        dynamic_count = nodeset.node_count_dynamic_max
        static_range, end = node_range(static_count) if static_count else (None, 0)
        dynamic_range, _ = (
            node_range(dynamic_count, start=end) if dynamic_count else (None, 0)
        )

        static_nodelist = f"{prefix}-{static_range}" if static_count else None
        dynamic_nodelist = f"{prefix}-{dynamic_range}" if dynamic_count else None
        return static_nodelist, dynamic_nodelist

    @lru_cache(maxsize=1)
    def static_nodelist(self):
        static_nodesets = (
            self.nodeset_lists(ns)[0]
            for ns in chain(self.cfg.nodeset.values(), self.cfg.nodeset_tpu.values())
        )
        return [static for static in static_nodesets if static is not None]

    @lru_cache(maxsize=None)
    def slurm_nodes(self):
        StateTuple = namedtuple("StateTuple", "base,flags")

        def make_node_tuple(node_line):
            """turn node,state line to (node, StateTuple(state))"""
            # state flags include: CLOUD, COMPLETING, DRAIN, FAIL, POWERED_DOWN,
            #   POWERING_DOWN
            node, fullstate = node_line.split(",")
            state = fullstate.split("+")
            state_tuple = StateTuple(state[0], set(state[1:]))
            return (node, state_tuple)

        cmd = (
            f"{self.scontrol} show nodes | "
            r"grep -oP '^NodeName=\K(\S+)|State=\K(\S+)' | "
            r"paste -sd',\n'"
        )
        node_lines = run(cmd, shell=True).stdout.rstrip().splitlines()
        nodes = {
            node: state
            for node, state in map(make_node_tuple, node_lines)
            if "CLOUD" in state.flags or "DYNAMIC_NORM" in state.flags
        }
        return nodes

    def slurm_node(self, nodename):
        return self.slurm_nodes().get(nodename)

    def cloud_nodes(self):
        static_nodes = []
        dynamic_nodes = []

        for nodeset in self.cfg.nodeset.values():
            static, dynamic = self.nodeset_lists(nodeset)
            if static is not None:
                static_nodes.extend(to_hostnames(static))
            if dynamic is not None:
                dynamic_nodes.extend(to_hostnames(dynamic))
        for nodeset in self.cfg.nodeset_tpu.values():
            static, dynamic = self.nodeset_lists(nodeset)
            if static is not None:
                static_nodes.extend(to_hostnames(static))
            if dynamic is not None:
                dynamic_nodes.extend(to_hostnames(dynamic))
        return static_nodes, dynamic_nodes

    def filter_nodes(self, nodes):
        static_nodes, dynamic_nodes = lkp.cloud_nodes()

        all_cloud_nodes = []
        all_cloud_nodes.extend(static_nodes)
        all_cloud_nodes.extend(dynamic_nodes)

        cloud_nodes = list(set(nodes).intersection(all_cloud_nodes))
        local_nodes = list(set(nodes).difference(all_cloud_nodes))

        return cloud_nodes, local_nodes

    def tpu_instances(self):
        res = []
        for ns in self.cfg.nodeset_tpu:
            tpuobj = TPU(ns)
            res.extend(tpuobj.list_node_names())
        return res

    @lru_cache(maxsize=1)
    def instances(self, project=None, slurm_cluster_name=None):
        slurm_cluster_name = slurm_cluster_name or self.cfg.slurm_cluster_name
        project = project or self.project
        instance_information_fields = [
            "advancedMachineFeatures",
            "cpuPlatform",
            "creationTimestamp",
            "disks",
            "disks",
            "fingerprint",
            "guestAccelerators",
            "hostname",
            "id",
            "kind",
            "labelFingerprint",
            "labels",
            "lastStartTimestamp",
            "lastStopTimestamp",
            "lastSuspendedTimestamp",
            "machineType",
            "metadata",
            "name",
            "networkInterfaces",
            "resourceStatus",
            "scheduling",
            "selfLink",
            "serviceAccounts",
            "shieldedInstanceConfig",
            "shieldedInstanceIntegrityPolicy",
            "sourceMachineImage",
            "status",
            "statusMessage",
            "tags",
            "zone",
            # "deletionProtection",
            # "startRestricted",
        ]
        if lkp.cfg.enable_slurm_gcp_plugins:
            slurm_gcp_plugins.register_instance_information_fields(
                lkp=lkp,
                project=project,
                slurm_cluster_name=slurm_cluster_name,
                instance_information_fields=instance_information_fields,
            )
        instance_information_fields = sorted(set(instance_information_fields))
        instance_fields = ",".join(instance_information_fields)
        fields = f"items.zones.instances({instance_fields}),nextPageToken"
        flt = f"labels.slurm_cluster_name={slurm_cluster_name} AND name:{slurm_cluster_name}-*"
        act = self.compute.instances()
        op = act.aggregatedList(project=project, fields=fields, filter=flt)

        def properties(inst):
            """change instance properties to a preferred format"""
            inst["zoneLink"] = inst["zone"]
            inst["zone"] = trim_self_link(inst["zone"])
            inst["machineTypeLink"] = inst["machineType"]
            inst["machineType"] = trim_self_link(inst["machineType"])
            # metadata is fetched as a dict of dicts like:
            # {'key': key, 'value': value}, kinda silly
            metadata = {i["key"]: i["value"] for i in inst["metadata"].get("items", [])}
            if "slurm_instance_role" not in metadata:
                return None
            inst["role"] = metadata["slurm_instance_role"]
            inst["metadata"] = metadata
            # del inst["metadata"]  # no need to store all the metadata
            return NSDict(inst)

        instances = {}
        while op is not None:
            result = ensure_execute(op)
            instance_iter = (
                (inst["name"], properties(inst))
                for inst in chain.from_iterable(
                    m["instances"] for m in result.get("items", {}).values()
                )
            )
            instances.update(
                {name: props for name, props in instance_iter if props is not None}
            )
            op = act.aggregatedList_next(op, result)
        return instances

    def instance(self, instance_name, project=None, slurm_cluster_name=None):
        instances = self.instances(
            project=project, slurm_cluster_name=slurm_cluster_name
        )
        return instances.get(instance_name)

    def describe_instance(self, instance_name, project=None, zone=None):
        project = project or self.project
        if zone is None:
            self.instances.cache_clear()
            inst = self.instance(instance_name, project=project)
            if inst is None:
                raise Exception(f"instance {instance_name} not found")
            zone = inst.zone
        op = self.compute.instances().get(
            instance=instance_name, project=project, zone=zone
        )
        info = ensure_execute(op)
        return NSDict(info)

    @lru_cache()
    def reservation(self, name):
        """
        See https://cloud.google.com/compute/docs/reference/rest/v1/reservations
        """
        resp = ensure_execute(
            self.compute.reservations().aggregatedList(
                project=self.project, filter=f"name={name}"
            )
        )

        reservation = None
        for _, e in resp["items"].items():
            for r in e.get("reservations", []):
                assert (
                    reservation is None
                ), f"multiple reservations '{name}' found in '{self.project}'."
                reservation = r

        assert (
            reservation is not None
        ), f"reservation '{name}' not found in '{self.project}'."
        return reservation

    @lru_cache(maxsize=1)
    def machine_types(self, project=None):
        project = project or self.project
        field_names = "name,zone,guestCpus,memoryMb,accelerators"
        fields = f"items.zones.machineTypes({field_names}),nextPageToken"

        machines = defaultdict(dict)
        act = self.compute.machineTypes()
        op = act.aggregatedList(project=project, fields=fields)
        while op is not None:
            result = ensure_execute(op)
            machine_iter = chain.from_iterable(
                m["machineTypes"]
                for m in result["items"].values()
                if "machineTypes" in m
            )
            for machine in machine_iter:
                name = machine["name"]
                zone = machine["zone"]
                machines[name][zone] = machine

            op = act.aggregatedList_next(op, result)
        return machines

    def machine_type(self, machine_type, project=None, zone=None):
        """ """
        custom_patt = re.compile(
            r"((?P<family>\w+)-)?custom-(?P<cpus>\d+)-(?P<mem>\d+)"
        )
        custom_match = custom_patt.match(machine_type)
        if zone:
            project = project or self.project
            machine_info = ensure_execute(
                self.compute.machineTypes().get(
                    project=project, zone=zone, machineType=machine_type
                )
            )
        elif custom_match is not None:
            groups = custom_match.groupdict()
            cpus, mem = (groups[k] for k in ["cpus", "mem"])
            machine_info = {
                "guestCpus": int(cpus),
                "memoryMb": int(mem),
            }
        else:
            machines = self.machine_types(project=project)
            machine_info = next(iter(machines[machine_type].values()), None)
            if machine_info is None:
                raise Exception(f"machine type {machine_type} not found")
        return NSDict(machine_info)

    def template_machine_conf(self, template_link, project=None, zone=None):
        template = self.template_info(template_link)
        if not template.machineType:
            temp_name = trim_self_link(template_link)
            raise Exception(f"instance template {temp_name} has no machine type")
        template.machine_info = self.machine_type(template.machineType, zone=zone)
        machine = template.machine_info

        machine_conf = NSDict()
        machine_conf.boards = 1  # No information, assume 1
        machine_conf.sockets = machine_type_sockets(template)
        machine_conf.threads_per_core = 1
        _div = 2 if getThreadsPerCore(template) == 1 else 1
        machine_conf.cpus = (
            int(machine.guestCpus / _div) if isSmt(template) else machine.guestCpus
        )
        machine_conf.cores_per_socket = int(machine_conf.cpus / machine_conf.sockets)
        # Because the actual memory on the host will be different than
        # what is configured (e.g. kernel will take it). From
        # experiments, about 16 MB per GB are used (plus about 400 MB
        # buffer for the first couple of GB's. Using 30 MB to be safe.
        gb = machine.memoryMb // 1024
        machine_conf.memory = machine.memoryMb - (400 + (30 * gb))
        return machine_conf

    @contextmanager
    def template_cache(self, writeback=False):
        flag = "c" if writeback else "r"
        err = None
        for wait in backoff_delay(0.125, timeout=60, count=20):
            try:
                cache = shelve.open(
                    str(self.template_cache_path), flag=flag, writeback=writeback
                )
                break
            except OSError as e:
                err = e
                log.debug(f"Failed to access template info cache: {e}")
                sleep(wait)
                continue
        else:
            # reached max_count of waits
            raise Exception(f"Failed to access cache file. latest error: {err}")
        try:
            yield cache
        finally:
            cache.close()

    @lru_cache(maxsize=None)
    def template_info(self, template_link, project=None):
        project = project or self.project
        template_name = trim_self_link(template_link)
        # split read and write access to minimize write-lock. This might be a
        # bit slower? TODO measure
        if self.template_cache_path.exists():
            with self.template_cache() as cache:
                if template_name in cache:
                    return NSDict(cache[template_name])

        template = ensure_execute(
            self.compute.instanceTemplates().get(
                project=project, instanceTemplate=template_name
            )
        ).get("properties")
        template = NSDict(template)
        # name and link are not in properties, so stick them in
        template.name = template_name
        template.link = template_link
        # TODO delete metadata to reduce memory footprint?
        # del template.metadata

        # translate gpus into an easier-to-read format
        machine_info = self.machine_type(template.machineType, project=project)
        if machine_info.accelerators:
            template.gpu_type = machine_info.accelerators[0].guestAcceleratorType
            template.gpu_count = machine_info.accelerators[0].guestAcceleratorCount
        elif template.guestAccelerators:
            template.gpu_type = template.guestAccelerators[0].acceleratorType
            template.gpu_count = template.guestAccelerators[0].acceleratorCount
        else:
            template.gpu_type = None
            template.gpu_count = 0

        # keep write access open for minimum time
        with self.template_cache(writeback=True) as cache:
            cache[template_name] = template.to_dict()
        # cache should be owned by slurm
        chown_slurm(self.template_cache_path)

        return template

    def clear_template_info_cache(self):
        with self.template_cache(writeback=True) as cache:
            cache.clear()
        self.template_info.cache_clear()

    def nodeset_map(self, hostnames: list):
        """Convert a list of nodes into a map of nodeset_name to hostnames"""
        nodeset_map = collections.defaultdict(list)
        for node in hostnames:
            nodeset_map[self.node_nodeset_name(node)].append(node)
        return nodeset_map


# Define late globals
lkp = Lookup()
cfg = load_config_file(CONFIG_FILE)
if not cfg:
    try:
        cfg = fetch_config_yaml()
    except Exception as e:
        log.warning(f"config not found in bucket: {e}")
    if cfg:
        save_config(cfg, CONFIG_FILE)

lkp = Lookup(cfg)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--partitions",
        "-p",
        help="The partition(s) to retrieve the TPU vmcount value for.",
    )
    args = parser.parse_args()
    if args.partitions:
        # useful exit code
        # partition does not exists in config.yaml, thus do not exist in slurm
        PART_INVALID = -1
        # in the same partition there are nodesets with different vmcounts
        DIFF_VMCOUNTS_SAME_PART = -2
        # partition is a list of partitions in which at least two of them have different vmcount
        DIFF_PART_DIFFERENT_VMCOUNTS = -3
        vmcounts = []
        # valid equals to 0 means that we are ok, otherwise it will be set to one of the previously defined exit codes
        valid = 0
        for part in args.partitions.split(","):
            if part not in lkp.cfg.partitions:
                valid = PART_INVALID
                break
            else:
                if part_is_tpu(part):
                    vmcount = get_vmcount_of_tpu_part(part)
                    if vmcount == -1:
                        valid = DIFF_VMCOUNTS_SAME_PART
                        break
                    vmcounts.append(vmcount)
                else:
                    vmcounts.append(0)
        # this means that there are different vmcounts for these partitions
        if valid == 0 and len(set(vmcounts)) != 1:
            valid = DIFF_PART_DIFFERENT_VMCOUNTS
        if valid != 0:
            print(f"VMCOUNT:{valid}")
        else:
            print(f"VMCOUNT:{vmcounts[0]}")
