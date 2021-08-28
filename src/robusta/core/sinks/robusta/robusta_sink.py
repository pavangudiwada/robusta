import base64
import json
import logging
import time
import traceback

from hikaru.model import Deployment, StatefulSetList, DaemonSetList, ReplicaSetList
from typing import List, Dict
from pydantic import BaseModel
import threading

from ..sink_config import SinkConfigBase
from ...model.env_vars import DISCOVERY_PERIOD_SEC
from ...model.services import ServiceInfo
from ...reporting.blocks import Finding
from .dal.supabase_dal import SupabaseDal
from ..sink_base import SinkBase
from ...discovery.top_service_resolver import TopServiceResolver


class RobustaSinkConfig(BaseModel):
    token: str


class RobustaToken(BaseModel):
    store_url: str
    api_key: str
    account_id: str
    email: str
    password: str


class RobustaSink(SinkBase):
    def __init__(
        self,
        sink_config: SinkConfigBase,
        cluster_name: str,
    ):
        super().__init__(sink_config)
        config = RobustaSinkConfig(**sink_config.params)
        robusta_token = RobustaToken(**json.loads(base64.b64decode(config.token)))
        self.dal = SupabaseDal(
            robusta_token.store_url,
            robusta_token.api_key,
            robusta_token.account_id,
            robusta_token.email,
            robusta_token.password,
            sink_config.sink_name,
            cluster_name,
        )
        # start service discovery
        self.__active = True
        self.__discovery_period_sec = DISCOVERY_PERIOD_SEC
        self.__services_cache: Dict[str, ServiceInfo] = {}
        self.__thread = threading.Thread(target=self.__discover_services)
        self.__thread.start()

    def stop(self):
        self.__active = False

    def write_finding(self, finding: Finding):
        self.dal.persist_finding(finding)

    # service discovery impl
    def __publish_service(self, serviceInfo: ServiceInfo):
        logging.debug(f"publishing to {self.sink_name} service {serviceInfo} ")
        self.dal.persist_service(serviceInfo)

    def __is_cached(self, service_info: ServiceInfo):
        cache_key = service_info.get_service_key()
        return self.__services_cache.get(cache_key) is not None

    def __publish_new_services(self, active_services: List):
        active_services_keys = set()
        for service in active_services:
            service_info = ServiceInfo(
                name=service.metadata.name,
                namespace=service.metadata.namespace,
                service_type=service.kind,
            )
            cache_key = service_info.get_service_key()
            active_services_keys.add(cache_key)
            cached_service = self.__services_cache.get(cache_key)
            if not cached_service or cached_service != service_info:
                self.__publish_service(service_info)
                self.__services_cache[cache_key] = service_info

        # delete cached services that aren't active anymore
        cache_keys = list(self.__services_cache.keys())
        for service_key in cache_keys:
            if service_key not in active_services_keys:
                del self.__services_cache[service_key]

        # handle delete services
        persisted_services = self.dal.get_active_services()
        deleted_services = [
            service for service in persisted_services if not self.__is_cached(service)
        ]
        for deleted_service in deleted_services:
            deleted_service.deleted = True
            self.__publish_service(deleted_service)

        # save the cached services in the resolver.
        TopServiceResolver.store_cached_services(list(self.__services_cache.values()))

    def __discover_services(self):
        while self.__active:
            try:
                current_services = Deployment.listDeploymentForAllNamespaces().obj.items
                current_services.extend(
                    StatefulSetList.listStatefulSetForAllNamespaces().obj.items
                )
                current_services.extend(
                    DaemonSetList.listDaemonSetForAllNamespaces().obj.items
                )
                current_services.extend(
                    [
                        rs
                        for rs in ReplicaSetList.listReplicaSetForAllNamespaces().obj.items
                        if rs.metadata.ownerReferences is None
                    ]
                )
                self.__publish_new_services(current_services)
            except Exception as e:
                logging.error(
                    f"Failed to run periodic service discovery for {self.sink_name}",
                    traceback.print_exc(),
                )

            time.sleep(self.__discovery_period_sec)

        logging.info(f"Service discovery for sink {self.sink_name} ended.")