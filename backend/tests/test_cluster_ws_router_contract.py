import unittest

from app.main import app


class ClusterWebSocketRouterContractTest(unittest.TestCase):
    def test_pod_websocket_routes_do_not_inherit_http_bearer_dependencies(self):
        """Pod WS 使用一次性会话 token，不能继承 HTTP Bearer 依赖。"""
        ws_paths = {
            "/api/clusters/{cluster_id}/pods/{namespace}/{name}/logs",
            "/api/clusters/{cluster_id}/pods/{namespace}/{name}/exec",
        }

        checked_paths = set()
        for route in app.routes:
            if getattr(route, "path", "") not in ws_paths:
                continue
            checked_paths.add(route.path)
            dependant = getattr(route, "dependant", None)
            dependency_names = {
                getattr(dependency.call, "__name__", "")
                for dependency in getattr(dependant, "dependencies", [])
            }

            self.assertNotIn("get_current_user", dependency_names)
            self.assertNotIn("_permission_guard", dependency_names)

        self.assertEqual(ws_paths, checked_paths)


if __name__ == "__main__":
    unittest.main()
