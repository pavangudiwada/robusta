from datetime import datetime

from robusta.core.playbooks.base_trigger import TriggerEvent
from robusta.integrations.kubernetes.api_client_utils import parse_kubernetes_datetime_to_ms
from robusta.integrations.kubernetes.autogenerated.triggers import PodChangeEvent, PodUpdateTrigger
from robusta.integrations.kubernetes.base_triggers import K8sTriggerEvent
from robusta.utils.rate_limiter import RateLimiter


class PodImagePullBackoffTrigger(PodUpdateTrigger):
    """
    :var rate_limit: Limit firing to once every `rate_limit` seconds
    :var fire_delay: Fire only if the pod is running for more than fire_delay seconds.
    """

    rate_limit: int = 14400
    fire_delay: int = 120

    def __init__(
        self,
        name_prefix: str = None,
        namespace_prefix: str = None,
        labels_selector: str = None,
        rate_limit: int = 14400,
        fire_delay: int = 120,
    ):
        super().__init__(
            name_prefix=name_prefix,
            namespace_prefix=namespace_prefix,
            labels_selector=labels_selector,
        )
        self.rate_limit = rate_limit
        self.fire_delay = fire_delay

    def should_fire(self, event: TriggerEvent, playbook_id: str):
        should_fire = super().should_fire(event, playbook_id)
        if not should_fire:
            return should_fire

        if not isinstance(event, K8sTriggerEvent):
            return False

        exec_event = self.build_execution_event(event, {})

        if not isinstance(exec_event, PodChangeEvent):
            return False

        pod = exec_event.get_pod()
        run_time_seconds = 0

        # startTime does not exist every time pod update is fired, like when the pod is just created
        if pod.status.startTime:
            run_time_seconds = (
                datetime.utcnow().timestamp() - parse_kubernetes_datetime_to_ms(pod.status.startTime) / 1000
            )

        # sometimes Image pull backoff fires falsely on pod startup
        # due to not loading a needed component like a secret before loading the image
        if self.fire_delay > run_time_seconds:
            return False

        statuses = pod.status.containerStatuses + pod.status.initContainerStatuses
        is_backoff = [
            container_status
            for container_status in statuses
            if container_status.state.waiting is not None
            and container_status.state.waiting.reason == "ImagePullBackOff"
        ]

        if not is_backoff:
            return False
        # Perform a rate limit for this pod according to the rate_limit parameter
        name = pod.metadata.ownerReferences[0].name if pod.metadata.ownerReferences else pod.metadata.name
        namespace = pod.metadata.namespace
        return RateLimiter.mark_and_test(
            f"PodImagePullBackoffTrigger_{playbook_id}",
            namespace + ":" + name,
            self.rate_limit,
        )
