import unittest
from unittest.mock import MagicMock, patch

from app.api.clusters import _open_pod_exec_stream


class ClusterExecStreamContractTest(unittest.TestCase):
    def test_open_pod_exec_stream_uses_core_v1_api_exec_method(self):
        """Pod exec 必须通过 CoreV1Api.connect_get_namespaced_pod_exec 打开交互流。"""
        api_client = object()
        core_v1 = MagicMock()
        stream_result = object()

        with patch("app.api.clusters.client.CoreV1Api", return_value=core_v1), patch(
            "app.api.clusters.k8s_stream", return_value=stream_result
        ) as stream_mock:
            result = _open_pod_exec_stream(
                api_client=api_client,
                namespace="default",
                name="nginx",
                command="/bin/sh",
                container="web",
                timeout_seconds=360,
            )

        self.assertIs(result, stream_result)
        stream_mock.assert_called_once()
        args, kwargs = stream_mock.call_args
        self.assertIs(args[0], core_v1.connect_get_namespaced_pod_exec)
        self.assertEqual(kwargs["namespace"], "default")
        self.assertEqual(kwargs["name"], "nginx")
        self.assertEqual(kwargs["container"], "web")
        self.assertEqual(kwargs["command"], ["/bin/sh", "-i"])
        self.assertFalse(kwargs["_preload_content"])


if __name__ == "__main__":
    unittest.main()
