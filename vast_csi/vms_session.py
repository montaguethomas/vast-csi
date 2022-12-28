import json
import requests
from pprint import pformat
from typing import ClassVar

from easypy.bunch import Bunch
from easypy.collections import shuffled
from easypy.tokens import (
    ROUNDROBIN,
    RANDOM,
    CONTROLLER_AND_NODE,
    CONTROLLER,
    NODE,
)


LOAD_BALANCING_STRATEGIES = {ROUNDROBIN, RANDOM}

from .logging import logger
from .exceptions import ApiError
from .configuration import Config, StorageClassOptions


class RESTSession(requests.Session):
    def __init__(self, *args, auth, base_url, ssl_verify, **kwargs):
        super().__init__(*args, **kwargs)
        self.base_url = base_url.rstrip("/")
        self.ssl_verify = ssl_verify
        self.auth = auth
        self.headers["Accept"] = "application/json"
        self.headers["Content-Type"] = "application/json"
        self.config = Config()

    def request(self, verb, api_method, *args, params=None, log_result=True, **kwargs):
        verb = verb.upper()
        api_method = api_method.strip("/")
        url = [self.base_url, api_method]
        url.extend(args)
        url += [""]  # ensures a '/' at the end
        url = "/".join(str(p) for p in url)
        logger.info(f">>> [{verb}] {url}")

        if "data" in kwargs:
            kwargs["data"] = json.dumps(kwargs["data"])

        if params or kwargs:
            for line in pformat(dict(kwargs, params=params)).splitlines():
                logger.info(f"    {line}")

        ret = super().request(
            verb, url, verify=self.ssl_verify, params=params, **kwargs
        )

        if ret.status_code in (400, 503):
            raise ApiError(response=ret)
        ret.raise_for_status()

        logger.info(f"<<< [{verb}] {url}")
        if ret.content:
            ret = Bunch.from_dict(ret.json())
            if log_result:
                for line in pformat(ret).splitlines():
                    logger.info(f"    {line}")
            else:
                size = len(ret) if isinstance(ret, (dict, tuple, list, str)) else '-'
                logger.info(f"{type(ret)[{size}]}")
        else:
            ret = None
        logger.info(f"--- [{verb}] {url}: Done")
        return ret

    def __getattr__(self, attr):
        if attr.startswith("_"):
            raise AttributeError(attr)

        def func(*args, log_result=True, **params):
            return self.request("get", attr, *args, params=params, log_result=log_result)

        func.__name__ = attr
        func.__qualname__ = f"{self.__class__.__qualname__}.{attr}"
        setattr(self, attr, func)
        return func


class VmsSession(RESTSession):
    """
    Communication with vms cluster.
    Operations over vip pools, quotas, snapshots etc.
    """

    _vip_round_robin_idx: ClassVar[int] = -1

    def get_vip(self, vip_pool_name: str, load_balancing: str = None):
        """
        Get vip pool by provided id.
        Returns:
            One of ips from provided vip pool according to provided load balancing strategy.
        """
        storage_options = StorageClassOptions.with_defaults()
        load_balancing = load_balancing or storage_options.load_balancing_strategy
        vips = [vip for vip in self.vips(log_result=False) if vip.vippool == vip_pool_name]
        if not vips:
            raise Exception(f"No vips in pool {vip_pool_name}")

        if load_balancing == ROUNDROBIN:
            self._vip_round_robin_idx = (self._vip_round_robin_idx + 1) % len(vips)
            vip = vips[self._vip_round_robin_idx]
        elif load_balancing == RANDOM:
            vip = shuffled(vips)[0]
        else:
            raise Exception(
                f"Invalid load_balancing mode: '{load_balancing}'"
            )

        logger.info(
            f"Using {load_balancing} - chose {vip.title}, currently connected to {vip.cnode}"
        )
        return vip.ip

    # ----------------------------
    # Quotas
    def list_quotas(self, max_entries) -> Bunch:
        """List of quotas"""
        return self.quotas(page_size=max_entries)

    def create_quota(self, data):
        """Create new quota"""
        return self.post("quotas", data=data)

    def get_quota(self, volume_id):
        """Get quota by volume id."""
        quotas = self.quotas(path__contains=volume_id)
        if not quotas:
            return
        elif len(quotas) > 1:
            names = ", ".join(sorted(q.name for q in quotas))
            raise Exception(f"Too many quotas on {volume_id}: {names}")
        else:
            return quotas[0]

    def get_quotas_by_path(self, path):
        path = path.rstrip("/")
        return self.quotas(path=path)

    def update_quota(self, quota_id, data):
        """Update existing quota."""
        self.patch(f"quotas/{quota_id}", data=data)

    def delete_quota(self, quota_id):
        """Delete quota"""
        self.delete(f"quotas/{quota_id}")

    # ----------------------------
    # Snapshots

    def snapshot_list(self, page_size):
        return self.snapshots(page_size=page_size)

    def has_snapshots(self, path):
        path = path.rstrip("/") + "/"
        ret = self.snapshots(path=path, page_size=10)  # we intentionally limit the number of results
        return ret.results

    def create_snapshot(self, data):
        """Create new snapshot."""
        return self.post("snapshots", data=data)

    def get_snapshot(self, snapshot_name=None, snapshot_id=None):
        """
        Get snapshot by name or by id.
        Only one argument should be provided.
        """
        if snapshot_name:
            ret = self.snapshots(name=snapshot_name)
            if len(ret) > 1:
                raise Exception(f"Too many snapshots named {snapshot_name}: ({len(ret)})")
            return ret[0]
        else:
            return self.snapshots(snapshot_id)

    def delete_snapshot(self, snapshot_id):
        self.delete(f"snapshots/{snapshot_id}")

    def get_by_token(self, token):
        """
        This method used to iterate over paginated resources (snapshots, quotas etc).
        Where after first request to resource list token for next page is returned.
        """
        return self.get(token)


class TestVmsSession(RESTSession):
    """RestSession simulation for sanity tests"""

    def get_vip(self, *_, **__) -> str:
        return self.config.nfs_server

    def get_quota(self, volume_id: str) -> "FakeQuota":
        """Create fake quota object which can simulate attributes of original Quota butch."""

        parent_self = self

        class FakeQuota:

            def __init__(self, volume_id):
                self._volume_id = volume_id

            def __str__(self):
                return "<< FakeQuota >>"

            @property
            def id(self):
                return self

            @property
            def path(self):
                return parent_self.config.nfs_export[self._volume_id]

        return FakeQuota(volume_id=volume_id)

    def delete_quota(self, quota: "FakeQuota"):
        """
        Delete all folders and files under '/csi-volumes/<volume id>
        Normally in this method quota id should be passed but here we abuse first position argument to
        pass FakeQuota which were initialized before and has '_volume_id' attribute.
        """
        self.config.controller_root_mount[quota._volume_id].delete()
        self.config.fake_quota_store[quota._volume_id].delete()
