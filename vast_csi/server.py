# Copyright 2015 gRPC authors.
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
"""The Python implementation of the GRPC helloworld.Greeter server."""

import os
import re
from concurrent import futures
from functools import wraps
from pprint import pformat
from datetime import datetime
import inspect
from tempfile import mkdtemp

import json
from json import JSONDecodeError
from plumbum import cmd
from plumbum import local, ProcessExecutionError
import grpc
from requests.exceptions import HTTPError

from easypy.tokens import ROUNDROBIN, RANDOM, CONTROLLER_AND_NODE, CONTROLLER, NODE
from easypy.misc import kwargs_resilient, at_least
from easypy.caching import cached_property
from easypy.bunch import Bunch
from easypy.exceptions import TException

from .logging import logger, init_logging
from .utils import (
    patch_traceback_format,
    get_mount,
    normalize_mount_options,
    parse_load_balancing_strategy,
    string_to_proto_timestamp
)
from . import csi_pb2_grpc
from .csi_pb2_grpc import ControllerServicer, NodeServicer, IdentityServicer
from . import csi_types as types
from .volume_builder import EmptyVolumeBuilder, VolumeFromSnapshotBuilder, TestVolumeBuilder
from .exceptions import Abort, ApiError, MissingParameter, MountFailed, VolumeAlreadyExists, SourceNotFound
from .vms_session import VmsSession, TestVmsSession
from .configuration import Config


CONF = None

################################################################
#
# Helpers
#
################################################################


FAILED_PRECONDITION = grpc.StatusCode.FAILED_PRECONDITION
INVALID_ARGUMENT = grpc.StatusCode.INVALID_ARGUMENT
ALREADY_EXISTS = grpc.StatusCode.ALREADY_EXISTS
NOT_FOUND = grpc.StatusCode.NOT_FOUND
ABORTED = grpc.StatusCode.ABORTED
UNKNOWN = grpc.StatusCode.UNKNOWN
OUT_OF_RANGE = grpc.StatusCode.OUT_OF_RANGE

SUPPORTED_ACCESS = [
    types.AccessModeType.SINGLE_NODE_WRITER,
    # types.AccessModeType.SINGLE_NODE_READER_ONLY,
    # types.AccessModeType.MULTI_NODE_READER_ONLY,
    # types.AccessModeType.MULTI_NODE_SINGLE_WRITER,
    types.AccessModeType.MULTI_NODE_MULTI_WRITER,
]


def mount(src, tgt, flags=""):
    executable = cmd.mount
    flags = [f.strip() for f in flags.split(",")]
    if CONF.mock_vast:
        flags += "port=2049,nolock,vers=3".split(",")
    flags = list(filter(None, flags))
    if flags:
        executable = executable["-o", ",".join(flags)]
    try:
        executable['-v', src, tgt] & logger.pipe_info("mount >>")
    except ProcessExecutionError as exc:
        raise MountFailed(detail=exc.stderr, src=src, tgt=tgt, mount_options=flags)


def _validate_capabilities(capabilities):
    for capability in capabilities:
        if capability.access_mode.mode not in SUPPORTED_ACCESS:
            raise Abort(
                INVALID_ARGUMENT,
                f"Unsupported access mode: {capability.access_mode.mode} (use {SUPPORTED_ACCESS})",
            )

        if not capability.HasField("mount"):
            pass
        elif not capability.mount.fs_type:
            pass
        elif capability.mount.fs_type != "ext4":
            raise Abort(
                INVALID_ARGUMENT,
                f"Unsupported file system type: {capability.mount.fs_type}",
            )


class Instrumented:

    SILENCED = ["Probe", "NodeGetCapabilities"]

    @classmethod
    def logged(cls, func):

        method = func.__name__
        log = logger.debug if (method in cls.SILENCED) else logger.info

        parameters = inspect.signature(func).parameters
        required_params = {
            name for name, p in parameters.items() if p.default is p.empty
        }
        required_params.discard("self")

        func = kwargs_resilient(func)

        @wraps(func)
        def wrapper(self, request, context):
            peer = context.peer()
            params = {fld.name: value for fld, value in request.ListFields()}
            missing = required_params - {"request", "context"} - set(params)

            log(f"{peer} >>> {method}:")

            if params:
                for line in pformat(params).splitlines():
                    log(f"    {line}")

            try:
                if missing:
                    msg = f'Missing required fields: {", ".join(sorted(missing))}'
                    logger.error(f"{peer} <<< {method}: {msg}")
                    raise Abort(INVALID_ARGUMENT, msg)

                ret = func(self, request=request, context=context, **params)
            except Abort as exc:
                logger.info(
                    f'{peer} <<< {method} ABORTED with {exc.code} ("{exc.message}")'
                )
                logger.debug("Traceback", exc_info=True)
                context.abort(exc.code, exc.message)
            except HTTPError as exc:
                reason = exc.response.reason
                status_code = exc.response.status_code
                text = exc.response.text.splitlines()[0]
                resource = exc.request.path_url
                logger.exception(f"Exception during {method}\n{exc.response.text}")
                context.abort(
                    UNKNOWN,
                    f"[{method}]. Unable to accomplish request to {resource}. {text}, <{reason}({status_code})>"
                )
            except TException as exc:
                # Any exception inherited from TException
                logger.exception(f"Exception during {method}")
                context.abort(UNKNOWN, f"[{method}]. {exc.render(color=False)}")
            except Exception as exc:
                logger.exception(f"Exception during {method}")
                text = str(exc)
                context.abort(UNKNOWN, f"[{method}]: {text}")
            if ret:
                log(f"{peer} <<< {method}:")
                for line in pformat(ret).splitlines():
                    log(f"    {line}")
            log(f"{peer} --- {method}: Done")
            return ret

        return wrapper

    @classmethod
    def __init_subclass__(cls):
        for name, _ in inspect.getmembers(cls.__base__, inspect.isfunction):
            if name.startswith("_"):
                continue
            func = getattr(cls, name)
            setattr(cls, name, cls.logged(func))
        super().__init_subclass__()


################################################################
#
# Identity
#
################################################################


class Identity(IdentityServicer, Instrumented):
    def __init__(self):
        self.capabilities = []
        self.controller = None
        self.node = None

    def GetPluginInfo(self, request, context):
        return types.InfoResp(
            name=CONF.plugin_name,
            vendor_version=CONF.plugin_version,
        )

    def GetPluginCapabilities(self, request, context):
        return types.CapabilitiesResp(
            capabilities=[
                types.Capability(service=types.Service(type=cap))
                for cap in self.capabilities
            ]
        )

    def Probe(self, request, context):
        if self.node:
            return types.ProbeRespOK
        elif CONF.mock_vast:
            return types.ProbeRespOK
        elif self.controller:
            try:
                self.controller.vms_session.get_vip()
            except ApiError as exc:
                raise Abort(FAILED_PRECONDITION, str(exc))
            return types.ProbeRespOK
        else:
            return types.ProbeRespNotReady


################################################################
#
# Controller
#
################################################################


class Controller(ControllerServicer, Instrumented):

    CAPABILITIES = [
        types.CtrlCapabilityType.CREATE_DELETE_VOLUME,
        types.CtrlCapabilityType.PUBLISH_UNPUBLISH_VOLUME,
        types.CtrlCapabilityType.LIST_VOLUMES,
        types.CtrlCapabilityType.EXPAND_VOLUME,
        types.CtrlCapabilityType.CREATE_DELETE_SNAPSHOT,
        types.CtrlCapabilityType.LIST_SNAPSHOTS,
        # types.CtrlCapabilityType.GET_CAPACITY,
        # types.CtrlCapabilityType.CLONE_VOLUME,
        # types.CtrlCapabilityType.PUBLISH_READONLY,
    ]

    @cached_property
    def vms_session(self):
        session_class = TestVmsSession if CONF.mock_vast else VmsSession
        session = session_class()
        logger.info(
            "Custom ssl certificates uploaded for use in VMS session."
            if CONF.vms_ssl_cert.exists() else
            "VMS session started without ssl certificates."
        )
        session.refresh_auth_token()
        return session

    def ControllerGetCapabilities(self):
        return types.CtrlCapabilityResp(
            capabilities=[
                types.CtrlCapability(rpc=types.CtrlCapability.RPC(type=rpc))
                for rpc in self.CAPABILITIES
            ]
        )

    def ValidateVolumeCapabilities(
        self,
        context,
        volume_id,
        volume_capabilities,
        volume_context=None,
        parameters=None,
    ):
        if not self.vms_session.get_quota(volume_id):
            raise Abort(NOT_FOUND, f"Volume {volume_id} does not exist")
        try:
            _validate_capabilities(volume_capabilities)
        except Abort as exc:
            return types.ValidateResp(message=exc.message)

        confirmed = types.ValidateResp.Confirmed(
            volume_context=volume_context,
            volume_capabilities=volume_capabilities,
            parameters=parameters,
        )

        return types.ValidateResp(confirmed=confirmed)

    def ListVolumes(self, starting_token=None, max_entries=None):
        if starting_token == "invalid-token":
            raise Abort(ABORTED, "Invalid starting_token")

        if starting_token:
            ret = self.vms_session.list_quotas(max_entries=max_entries)
        else:
            ret = self.vms_session.get_by_token(token=starting_token)
        return types.ListResp(
            next_token=ret.next_token,
            entries=[
                types.ListResp.Entry(
                    volume=types.Volume(
                        capacity_bytes=quota.hard_limit,
                        volume_id=self._to_volume_id(quota.path),
                        volume_context=dict(quota_id=str(quota.id)),
                    )
                )
                for quota in ret.results
            ],
        )

    def CreateVolume(
        self,
        name,
        volume_capabilities,
        capacity_range=None,
        parameters=None,
        volume_content_source=None,
        ephemeral_volume_name=None,
    ):
        _validate_capabilities(volume_capabilities)
        parameters = parameters or dict()

        try:
            mount_capability = next(cap for cap in volume_capabilities if cap.HasField("mount"))
            mount_flags = mount_capability.mount.mount_flags
            mount_options = ",".join(mount_flags)
            # normalize mount options (remove spaces, brackets etc)
            mount_options = ",".join(re.sub(r"[\[\]]", "", mount_options).replace(",", " ").split())
        except StopIteration:
            mount_options = ""

        # Take appropriate builder for volume, snapshot or test builder
        if CONF.mock_vast:
            root_export = volume_name_fmt = lb_strategy = view_policy = vip_pool_name = mount_options = qos_policy = ""
            builder = TestVolumeBuilder

        else:
            if not (root_export := parameters.get("root_export")):
                raise MissingParameter(param="root_export")
            if not (view_policy := parameters.get("view_policy")):
                raise MissingParameter(param="view_policy")
            if not (vip_pool_name := parameters.get("vip_pool_name")):
                raise MissingParameter(param="vip_pool_name")
            volume_name_fmt = parameters.get("volume_name_fmt", CONF.name_fmt)
            lb_strategy = parameters.get("lb_strategy", CONF.load_balancing)
            qos_policy = parameters.get("qos_policy")

            if not volume_content_source:
                builder = EmptyVolumeBuilder

            elif volume_content_source.snapshot.snapshot_id:
                builder = VolumeFromSnapshotBuilder

            else:
                raise ValueError(
                    "Invalid condition. Either volume_content_source"
                    " or test environment variable should be provided"
                )

        # Create volume, volume from snapshot or mount local path (for testing purposes)
        # depends on chosen builder.
        builder = builder(
            controller=self,
            configuration=CONF,
            name=name,
            capacity_range=capacity_range,
            pvc_name=parameters.get("csi.storage.k8s.io/pvc/name"),
            pvc_namespace=parameters.get("csi.storage.k8s.io/pvc/namespace"),
            volume_content_source=volume_content_source,
            ephemeral_volume_name=ephemeral_volume_name,
            root_export=root_export,
            volume_name_fmt=volume_name_fmt,
            view_policy=view_policy,
            vip_pool_name=vip_pool_name,
            mount_options=mount_options,
            lb_strategy=lb_strategy,
            qos_policy=qos_policy,
        )
        try:
            volume = builder.build_volume()
        except SourceNotFound as exc:
            raise Abort(NOT_FOUND, exc.message)
        except VolumeAlreadyExists as exc:
            raise Abort(ALREADY_EXISTS, exc.message)
        return types.CreateResp(volume=volume)

    def _delete_data_from_storage(self, path, tenant_id):
        if self.vms_session.is_trash_api_usable():
            logger.info(f"Use trash API to delete {path}")
            self.vms_session.delete_folder(path, tenant_id)
            return

        logger.info(f"Use local mounting to delete {path}")
        path = local.path(path)
        volume_id = path.name
        view_policy = self.vms_session.ensure_view_policy(policy_name=CONF.deletion_view_policy)
        nfs_server = self.vms_session.get_vip(vip_pool_name=CONF.deletion_vip_pool, tenant_id=view_policy.tenant_id)

        logger.info(f"Creating temporary base view.")
        with self.vms_session.temp_view(path.dirname, view_policy.id, view_policy.tenant_id) as base_view:

            mount_spec = f"{nfs_server}:{base_view.alias}"
            mounted = False
            tmpdir = local.path(mkdtemp())  # convert string to local.path
            tmpdir['.csi-unmounted'].touch()

            try:
                mount(mount_spec, tmpdir, flags=",".join(CONF.mount_options))
                assert not tmpdir['.csi-unmounted'].exists()
                mounted = True

                if tmpdir[volume_id].exists():
                    logger.info(f"deleting {tmpdir[volume_id]}")
                    tmpdir[volume_id].delete()
                    logger.info(f"done deleting {tmpdir[volume_id]}")
                else:
                    logger.info(f"already deleted {tmpdir[volume_id]}")
            except FileNotFoundError as exc:
                if 'No such file or directory' in str(exc):
                    logger.warning(
                        'It appears that multiple processes are attempting to clean a single directory,'
                        ' leading to unforeseeable concurrent access to the identical file or directory.'
                        ' The cleaning process will be repeated.'
                    )
                    raise Abort(
                        ABORTED,
                        f"Concurrent access to an identical file/directory has been detected."
                        f" A new attempt will be made.",
                    )
                else:
                    raise
            except OSError as exc:
                if 'not empty' in str(exc):
                    for i, item in enumerate(tmpdir[volume_id].list()):
                        if i > 9:
                            logger.debug(" ...")
                            break
                        logger.warning(f" - {item}")
                raise
            finally:
                if mounted:
                    cmd.umount['-v', tmpdir] & logger.pipe_info("umount >>", retcode=None)  # don't fail if not mounted
                os.remove(tmpdir['.csi-unmounted'])  # will fail if still mounted somehow
                os.rmdir(tmpdir)  # will fail if not empty directory

    def DeleteVolume(self, volume_id):
        if quota := self.vms_session.get_quota(volume_id):
            try:
                self._delete_data_from_storage(quota.path, quota.tenant_id)
            except OSError as exc:
                if 'not empty' not in str(exc):
                    raise
                if snaps := self.vms_session.has_snapshots(quota.path):
                    # this is expected when the volume has snapshots
                    logger.info(f"{quota.path} will remain due to remaining {len(snaps)} snapshots")
                else:
                    raise
            logger.info(f"Data removed: {quota.path}")

            self.vms_session.delete_view_by_path(quota.path)
            logger.info(f"View removed: {quota.path}")

            self.vms_session.delete_quota(quota.id)
            logger.info(f"Quota removed: {quota.id}")

        logger.info(f"Removed volume: {volume_id}")
        return types.DeleteResp()

    def ControllerPublishVolume(
        self, node_id, volume_id, volume_capability, volume_context=None
    ):
        volume_context = volume_context or dict()
        _validate_capabilities([volume_capability])

        if CONF.mock_vast:
            root_export = CONF.sanity_test_nfs_export
        else:
            root_export = local.path(volume_context["root_export"])

        load_balancing = parse_load_balancing_strategy(volume_context.get("load_balancing", CONF.load_balancing))

        # Build export path for snapshot or volume
        if snapshot_base_path := volume_context.get("snapshot_base_path"):
            # Snapshot
            quota_path_fragment = snapshot_base_path.split("/")[0]
            export_path = str(root_export[snapshot_base_path])
        else:
            # Volume
            quota_path_fragment = volume_id
            export_path = str(root_export[volume_id])

        vip_pool_name = None if CONF.mock_vast else volume_context["vip_pool_name"]
        if not (quota := self.vms_session.get_quota(quota_path_fragment)):
            raise Abort(NOT_FOUND, f"Unknown volume: {quota_path_fragment}")

        if CONF.csi_sanity_test and CONF.node_id != node_id:
            # for a test that tries to fake a non-existent node
            raise Abort(NOT_FOUND, f"Unknown volume: {node_id}")

        nfs_server_ip = self.vms_session.get_vip(
            vip_pool_name=vip_pool_name,
            load_balancing=load_balancing,
            tenant_id=quota.tenant_id,
        )

        return types.CtrlPublishResp(
            publish_context=dict(
                export_path=export_path,
                nfs_server_ip=nfs_server_ip,
            )
        )

    def ControllerUnpublishVolume(self, node_id, volume_id):
        return types.CtrlUnpublishResp()

    def ControllerExpandVolume(self, volume_id, capacity_range):
        requested_capacity = capacity_range.required_bytes

        if not (quota := self.vms_session.get_quota(volume_id)):
            raise Abort(NOT_FOUND, f"Not found quota with id: {volume_id}")

        existing_capacity = quota.hard_limit
        if requested_capacity <= existing_capacity:
            capacity_bytes = existing_capacity
        else:
            try:
                self.vms_session.update_quota(
                    quota_id=quota.id, data=dict(hard_limit=requested_capacity)
                )
            except ApiError as exc:
                raise Abort(OUT_OF_RANGE, f"Failed updating quota {quota.id}: {exc}")
            capacity_bytes = requested_capacity

        return types.CtrlExpandResp(
            capacity_bytes=capacity_bytes,
            node_expansion_required=False,
        )

    def CreateSnapshot(self, source_volume_id, name, parameters=None):

        parameters = parameters or dict()
        volume_id = source_volume_id
        if not (quota := self.vms_session.get_quota(volume_id)):
            raise Abort(NOT_FOUND, f"Unknown volume: {volume_id}")

        if CONF.mock_vast:

            try:
                with CONF.fake_snapshot_store[name].open("rb") as f:
                    snp = types.Snapshot()
                    snp.ParseFromString(f.read())
                if snp.source_volume_id != volume_id:
                    raise Abort(
                        ALREADY_EXISTS, f"Snapshot name '{name}' is already taken"
                    )
            except FileNotFoundError:
                ts = types.Timestamp()
                ts.FromDatetime(datetime.utcnow())
                snp = types.Snapshot(
                    size_bytes=0,  # indicates 'unspecified'
                    snapshot_id=name,
                    source_volume_id=volume_id,
                    creation_time=ts,
                    ready_to_use=True,
                )
                with CONF.fake_snapshot_store[name].open("wb") as f:
                    f.write(snp.SerializeToString())
        else:
            # Create snapshot using the same path as quota has.
            path = quota.path
            tenant_id = quota.tenant_id
            snapshot_name = parameters["csi.storage.k8s.io/volumesnapshot/name"]
            snapshot_namespace = parameters[
                "csi.storage.k8s.io/volumesnapshot/namespace"
            ]
            snapshot_name_fmt = parameters.get("snapshot_name_fmt", CONF.name_fmt)
            snapshot_name = snapshot_name_fmt.format(
                namespace=snapshot_namespace, name=snapshot_name, id=name
            )
            snapshot_name = snapshot_name.replace(":", "-").replace("/", "-")
            try:
                snap = self.vms_session.ensure_snapshot(snapshot_name=snapshot_name, path=path, tenant_id=tenant_id)
            except ApiError as exc:
                handled = False
                if exc.response.status_code == 400:
                    try:
                        [(k, [v])] = exc.response.json().items()
                    except (ValueError, JSONDecodeError):
                        pass
                    else:
                        if (k, v) == ("name", "This field must be unique."):
                            snap = self.vms_session.get_snapshot(snapshot_name=snapshot_name)
                            if snap.path != path:
                                raise Abort(
                                    ALREADY_EXISTS,
                                    f"Snapshot name '{name}' is already taken",
                                ) from None
                            else:
                                handled = True
                if not handled:
                    raise Abort(INVALID_ARGUMENT, str(exc))

            snp = types.Snapshot(
                size_bytes=0,  # indicates 'unspecified'
                snapshot_id=str(snap.id),
                source_volume_id=volume_id,
                creation_time=string_to_proto_timestamp(snap.created),
                ready_to_use=True,
            )

        return types.CreateSnapResp(snapshot=snp)

    def DeleteSnapshot(self, snapshot_id):
        if CONF.mock_vast:
            CONF.fake_snapshot_store[snapshot_id].delete()
        else:
            snapshot = self.vms_session.get_snapshot(snapshot_id=snapshot_id)
            self.vms_session.delete_snapshot(snapshot_id)
            if self.vms_session.get_quotas_by_path(snapshot.path):
                pass  # quotas still exist
            elif self.vms_session.has_snapshots(snapshot.path):
                pass  # other snapshots still exist
            else:
                logger.info(f"last snapshot for {snapshot.path}, and no more quotas - let's delete this directory")
                self._delete_data_from_storage(snapshot.path, snapshot.tenant_id)

        return types.DeleteSnapResp()

    @classmethod
    def _to_volume_id(cls, path):
        vol_id = str(local.path(path).relative_to(CONF.sanity_test_nfs_export))
        return None if vol_id.startswith("..") else vol_id

    def ListSnapshots(
        self,
        max_entries=None,
        starting_token=None,
        source_volume_id=None,
        snapshot_id=None,
    ):
        if CONF.mock_vast:
            starting_inode = int(starting_token) if starting_token else 0
            snaps = (d for d in os.scandir(CONF.fake_snapshot_store) if d.is_file())
            snaps = sorted(snaps, key=lambda d: d.inode())
            logger.info(f"Got {len(snaps)} snapshots in {CONF.fake_snapshot_store}")
            start_idx = 0

            logger.info(f"Skipping to {starting_inode}")
            for start_idx, d in enumerate(snaps):
                if d.inode() > starting_inode:
                    break
            del snaps[:start_idx]

            def to_snapshot(dentry):
                with local.path(dentry.path).open("rb") as f:
                    snap = types.Snapshot()
                    snap.ParseFromString(f.read())
                if source_volume_id and snap.source_volume_id != source_volume_id:
                    return
                if snapshot_id and snap.snapshot_id != snapshot_id:
                    return
                return snap, dentry.inode()

            snaps = list(filter(None, map(to_snapshot, snaps)))
            remain = 0
            if max_entries:
                remain = at_least(0, len(snaps) - max_entries)
                snaps = snaps[:max_entries]

            next_token = str(snaps[-1][1]) if remain else None
            return types.ListSnapResp(
                next_token=next_token,
                entries=[types.SnapEntry(snapshot=snap) for snap, _ in snaps],
            )
        else:
            page_size = max_entries or 250

            if starting_token:
                ret = self.vms_session.get_by_token(starting_token)
            elif not snapshot_id:
                ret = self.vms_session.snapshots(page_size=page_size)
            else:
                snap = self.vms_session.snapshots(snapshot_id)
                return types.ListSnapResp(next_token=None, entries=[types.SnapEntry(
                    snapshot=types.Snapshot(
                        size_bytes=0,  # indicates 'unspecified'
                        snapshot_id=str(snap.id),
                        source_volume_id=self._to_volume_id(snap.path) or "n/a",
                        creation_time=string_to_proto_timestamp(snap.created),
                        ready_to_use=True,
                    ))])

            return types.ListSnapResp(next_token=ret.next, entries=[types.SnapEntry(
                snapshot=types.Snapshot(
                    size_bytes=0,  # indicates 'unspecified'
                    snapshot_id=str(snap.id),
                    source_volume_id=self._to_volume_id(snap.path) or "n/a",
                    creation_time=string_to_proto_timestamp(snap.created),
                    ready_to_use=True,
                )) for snap in ret.results])


################################################################
#
# Node
#
################################################################


class Node(NodeServicer, Instrumented):

    CAPABILITIES = [
        # types.NodeCapabilityType.STAGE_UNSTAGE_VOLUME,
        # types.NodeCapabilityType.GET_VOLUME_STATS,
    ]

    def NodeGetCapabilities(self):
        return types.NodeCapabilityResp(
            capabilities=[
                types.NodeCapability(rpc=types.NodeCapability.RPC(type=rpc))
                for rpc in self.CAPABILITIES
            ]
        )

    def NodePublishVolume(
        self,
        volume_id,
        target_path,
        volume_capability=None,
        publish_context=None,
        readonly=False,
        volume_context=None,
    ):
        volume_context = volume_context or dict()
        if (
            is_ephemeral := volume_context
            and volume_context.get("csi.storage.k8s.io/ephemeral") == "true"
        ):
            from .quantity import parse_quantity

            eph_volume_name_fmt = volume_context.get("eph_volume_name_fmt", CONF.name_fmt)
            if "size" in volume_context:
                required_bytes = int(parse_quantity(volume_context["size"]))
                capacity_range = Bunch(required_bytes=required_bytes)
            else:
                capacity_range = None
            pod_uid = volume_context["csi.storage.k8s.io/pod.uid"]
            pod_name = volume_context["csi.storage.k8s.io/pod.name"]
            pod_namespace = volume_context["csi.storage.k8s.io/pod.namespace"]
            eph_volume_name = eph_volume_name_fmt.format(
                namespace=pod_namespace, name=pod_name, id=pod_uid
            )

            controller = Controller()
            controller.CreateVolume.__wrapped__(
                controller,
                name=volume_id,
                volume_capabilities=[],
                ephemeral_volume_name=eph_volume_name,
                capacity_range=capacity_range,
                parameters=volume_context
            )
            resp = controller.ControllerPublishVolume.__wrapped__(
                controller,
                node_id=CONF.node_id,
                volume_id=volume_id,
                volume_capability=volume_capability,
                volume_context=volume_context,
            )
            publish_context = resp.publish_context
        elif not volume_capability:
            raise Abort(INVALID_ARGUMENT, "missing 'volume_capability'")

        nfs_server_ip = publish_context["nfs_server_ip"]

        schema = "1" if not volume_context else volume_context.get("schema", "1")
        if schema == "2":
            export_path = volume_context["export_path"]
        else:
            export_path = publish_context["export_path"]
        mount_spec = f"{nfs_server_ip}:{export_path}"

        _validate_capabilities([volume_capability])
        target_path = local.path(target_path)

        if not target_path.is_dir():
            pass
        elif found_mount := get_mount(target_path):
            opts = set(found_mount.opts.split(","))
            is_readonly = "ro" in opts
            if found_mount.device != mount_spec:
                raise Abort(
                    ALREADY_EXISTS,
                    f"Volume already mounted from {found_mount.device} instead of {mount_spec}",
                )
            elif is_readonly != readonly:
                raise Abort(
                    ALREADY_EXISTS,
                    f"Volume already mounted as {'readonly' if is_readonly else 'readwrite'}",
                )
            else:
                logger.info(f"{volume_id} is already mounted: {found_mount}")
                return types.NodePublishResp()

        target_path.mkdir()
        with target_path[".vast-csi-meta"].open("w") as f:
            json.dump(dict(volume_id=volume_id, is_ephemeral=is_ephemeral), f)
        logger.info(f"created: {target_path}")

        flags = ["ro"] if readonly else []
        if volume_capability.mount.mount_flags:
            flags += volume_capability.mount.mount_flags
        else:
            flags += normalize_mount_options(volume_context.get("mount_options", ""))
        mount(mount_spec, target_path, flags=",".join(flags))
        logger.info(f"mounted: {target_path} flags: {flags}")
        return types.NodePublishResp()

    def NodeUnpublishVolume(self, target_path):
        target_path = local.path(target_path)

        if not target_path.exists():
            logger.info(f"{target_path} does not exist - no need to remove")
        else:
            # make sure we're really unmounted before we delete anything
            for i in range(CONF.unmount_attempts):
                if not get_mount(target_path):
                    logger.info(f"{target_path} is not mounted")
                    break
                try:
                    local.cmd.umount(target_path)
                except ProcessExecutionError as exc:
                    if "not mounted" in exc.stderr:
                        logger.info(f"umount failed - {target_path} is not mounted (race?)")
                        break
                    raise
            else:
                raise Abort(
                    UNKNOWN,
                    f"Stuck in unmount loop of {target_path} too many times ({CONF.unmount_attempts})",
                )

            logger.info(f"Deleting {target_path}")
            if target_path[".vast-csi-meta"].exists():
                with target_path[".vast-csi-meta"].open("r") as f:
                    meta = json.load(f)
                if meta.get("is_ephemeral"):
                    controller = Controller()
                    controller.DeleteVolume.__wrapped__(controller, meta["volume_id"])

            if target_path[".vast-csi-meta"].exists():
                os.remove(target_path[".vast-csi-meta"])
            os.rmdir(target_path)  # don't use plumbum's .delete to avoid the dangerous rmtree
            logger.info(f"{target_path} removed successfully")
        return types.NodeUnpublishResp()

    def NodeGetInfo(self):
        return types.NodeInfoResp(node_id=CONF.node_id)


################################################################
#
# Entrypoint
#
################################################################


def serve():
    patch_traceback_format()
    global CONF
    CONF = Config()
    init_logging(level=CONF.log_level)
    logger.info("%s: %s (%s)", CONF.plugin_name, CONF.plugin_version, CONF.git_commit)

    if not CONF.ssl_verify:
        import urllib3

        urllib3.disable_warnings()

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=CONF.worker_threads))

    identity = Identity()
    csi_pb2_grpc.add_IdentityServicer_to_server(identity, server)

    identity.capabilities.append(types.ExpansionType.ONLINE)

    if CONF.mode in {CONTROLLER, CONTROLLER_AND_NODE}:
        identity.controller = Controller()
        identity.capabilities.append(types.ServiceType.CONTROLLER_SERVICE)
        csi_pb2_grpc.add_ControllerServicer_to_server(identity.controller, server)
        CONF.fake_quota_store.mkdir()
        CONF.fake_snapshot_store.mkdir()

    if CONF.mode in {NODE, CONTROLLER_AND_NODE}:
        identity.node = Node()
        csi_pb2_grpc.add_NodeServicer_to_server(identity.node, server)

    server.add_insecure_port(CONF.endpoint)
    server.start()

    logger.info(f"Server started as '{CONF.mode}', listening on {CONF.endpoint}, spawned threads {CONF.worker_threads}")
    server.wait_for_termination()
