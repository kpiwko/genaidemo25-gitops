#!/usr/bin/env python3
"""Offline manifest tests for the headless OpenWebUI work (Task 5).

Fully local and offline: kustomize renders (kustomize or `oc kustomize`
fallback) of the base, the custom-domain overlay, and the certificate-only
phase are asserted semantically. No cluster access, no network writes, no
real credentials, no Secret reads.

Covers:
  - the PostSync provisioning Job (hook policy, SA, automount, image pin,
    env contract, readOnly mounts, no PVC, bounded spec, resources);
  - the generated content-hash ConfigMap `openwebui-workshop-provision`
    (both sole inputs present, name hash-rewritten into the Job, content
    equals the Git-tracked files, hash changes when an input changes);
  - preservation invariants: PVC/model sources/TLS objects unchanged,
    plural connection URLs + ENABLE_SIGNUP=false, overlay adds exactly its
    5 custom-domain objects, base independence (semantic, NOT
    whole-base-equals-HEAD), certificate-only builds exactly 2 docs;
  - no plaintext secret material in any render.

Run:
    python3 -m unittest module-hsworkshop/tests/test_headless_manifests.py
or from module-hsworkshop:
    python3 -m unittest tests.test_headless_manifests
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MODULE = os.path.join(REPO_ROOT, "module-hsworkshop")
BASE = os.path.join(MODULE, "install")
OVERLAY = os.path.join(MODULE, "overlays", "custom-domain")
CERT_ONLY = os.path.join(OVERLAY, "certificate-only")
PROVISIONING = os.path.join(MODULE, "provisioning")
PROVISION_JOB = os.path.join(BASE, "openwebui-provisioning.yaml")

OPENWEBUI_IMAGE = (
    "ghcr.io/open-webui/open-webui@"
    "sha256:0b399a8ba34558e181d316750455f2a95376a38cd60a61b9c942e138898944bd"
)


def _kustomize_binary():
    if shutil.which("kustomize"):
        return ["kustomize", "build"]
    if shutil.which("oc"):
        return ["oc", "kustomize"]
    raise unittest.SkipTest("neither kustomize nor oc is available")  # pragma: no cover


def build(path):
    """Render a kustomization directory with the available builder."""
    cmd = _kustomize_binary() + [path]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise AssertionError(f"kustomize build {path} failed:\n{proc.stderr}")
    return [d for d in yaml.safe_load_all(proc.stdout) if d]


def key(d):
    m = d.get("metadata", {})
    return (d.get("apiVersion"), d.get("kind"), m.get("namespace", ""), m.get("name"))


def env_map(doc):
    containers = doc["spec"]["template"]["spec"]["containers"]
    out = {}
    for c in containers:
        for e in c.get("env", []):
            out.setdefault(e["name"], e)
    return out


class BaseRenderTestCase(unittest.TestCase):
    """One shared base render for all base-level assertions."""

    @classmethod
    def setUpClass(cls):
        cls.base = build(BASE)

    def find(self, kind, name):
        matches = [d for d in self.base if d.get("kind") == kind
                   and d["metadata"]["name"] == name]
        self.assertEqual(len(matches), 1, f"expected exactly one {kind}/{name}")
        return matches[0]


class TestProvisioningJob(BaseRenderTestCase):
    def test_job_present_with_exact_identity(self):
        job = self.find("Job", "openwebui-workshop-provision")
        self.assertEqual(job["apiVersion"], "batch/v1")
        self.assertEqual(job["metadata"]["namespace"], "hsworkshop")

    def test_postsync_hook_before_hook_creation_only(self):
        job = self.find("Job", "openwebui-workshop-provision")
        ann = job["metadata"]["annotations"]
        self.assertEqual(ann["argocd.argoproj.io/hook"], "PostSync")
        # BeforeHookCreation ONLY: the last result is retained between syncs
        # (no HookSucceeded/HookFailed deletion policies).
        self.assertEqual(ann["argocd.argoproj.io/hook-delete-policy"],
                         "BeforeHookCreation")
        for banned in ("HookSucceeded", "HookFailed"):
            self.assertNotIn(banned, ann["argocd.argoproj.io/hook-delete-policy"])

    def test_existing_sa_no_automount_no_extra_rbac(self):
        spec = self.find("Job", "openwebui-workshop-provision")["spec"]["template"]["spec"]
        self.assertEqual(spec["serviceAccountName"], "hsworkshop-anyuid")
        self.assertFalse(spec["automountServiceAccountToken"])
        # No RBAC object may be added for the provisioning Job: the only
        # RBAC in the base is the pre-existing hsworkshop-anyuid SCC binding.
        rbac = [d for d in self.base if d["kind"] in ("Role", "RoleBinding",
                                                      "ClusterRole", "ClusterRoleBinding")]
        names = {(d["kind"], d["metadata"]["name"]) for d in rbac}
        self.assertEqual(names, {("ClusterRoleBinding", "hsworkshop-anyuid")},
                         f"unexpected RBAC objects: {names}")

    def test_bounded_job_spec(self):
        spec = self.find("Job", "openwebui-workshop-provision")["spec"]
        self.assertEqual(spec["backoffLimit"], 3)
        self.assertEqual(spec["activeDeadlineSeconds"], 600)
        self.assertEqual(spec["template"]["spec"]["restartPolicy"], "Never")

    def test_pinned_image_explicit_python_command(self):
        spec = self.find("Job", "openwebui-workshop-provision")["spec"]["template"]["spec"]
        self.assertEqual(len(spec["containers"]), 1)
        c = spec["containers"][0]
        self.assertEqual(c["image"], OPENWEBUI_IMAGE)
        # Same digest as the OpenWebUI Deployment (no upgrade, no new image).
        dep = self.find("Deployment", "openwebui")
        dep_img = dep["spec"]["template"]["spec"]["containers"][0]["image"]
        self.assertEqual(c["image"], dep_img)
        self.assertEqual(c["command"], ["python", "/provision/provision_openwebui.py"])

    def test_env_contract_exact(self):
        env = env_map(self.find("Job", "openwebui-workshop-provision"))
        self.assertEqual(set(env), {"OPENWEBUI_URL", "WEBUI_ADMIN_EMAIL",
                                    "WEBUI_ADMIN_PASSWORD", "PRESETS_PATH"})
        self.assertEqual(env["OPENWEBUI_URL"]["value"], "http://openwebui:8080")
        self.assertEqual(env["PRESETS_PATH"]["value"],
                         "/provision/configuration-models.json")
        for name in ("WEBUI_ADMIN_EMAIL", "WEBUI_ADMIN_PASSWORD"):
            ref = env[name]["valueFrom"]["secretKeyRef"]
            self.assertEqual(ref, {"name": "openwebui-admin-bootstrap-secret",
                                   "key": name}, ref)

    def test_readonly_configmap_mount_no_pvc(self):
        spec = self.find("Job", "openwebui-workshop-provision")["spec"]["template"]["spec"]
        mounts = spec["containers"][0]["volumeMounts"]
        self.assertEqual(mounts, [{"name": "provision-script",
                                   "mountPath": "/provision",
                                   "readOnly": True}])
        vols = spec["volumes"]
        self.assertEqual(len(vols), 1)
        self.assertIn("configMap", vols[0])
        self.assertNotIn("persistentVolumeClaim", vols[0])
        self.assertEqual(vols[0]["name"], "provision-script")
        # No PVC may be mounted anywhere in the Job pod spec.
        self.assertNotIn("persistentVolumeClaim", str(spec))

    def test_conservative_resources(self):
        c = self.find("Job", "openwebui-workshop-provision")["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(c["resources"]["requests"], {"cpu": "100m", "memory": "128Mi"})
        self.assertEqual(c["resources"]["limits"], {"cpu": "100m", "memory": "128Mi"})

    def test_no_plaintext_credentials_in_render(self):
        job = self.find("Job", "openwebui-workshop-provision")
        self.assertNotIn(str(job), "password=")
        text = yaml.safe_dump(job)
        for banned in ("BEGIN PRIVATE KEY", "BEGIN CERTIFICATE"):
            self.assertNotIn(banned, text)
        # Env must be refs, not values.
        for e in job["spec"]["template"]["spec"]["containers"][0]["env"]:
            if e["name"] in ("WEBUI_ADMIN_EMAIL", "WEBUI_ADMIN_PASSWORD"):
                self.assertIn("valueFrom", e)


class TestProvisioningConfigMap(BaseRenderTestCase):
    def test_hash_cm_generated_with_both_sole_inputs(self):
        cms = [d for d in self.base if d["kind"] == "ConfigMap"
               and d["metadata"]["name"].startswith("openwebui-workshop-provision")]
        self.assertEqual(len(cms), 1, "expected exactly one provisioning CM")
        cm = cms[0]
        self.assertEqual(cm["metadata"]["namespace"], "hsworkshop")
        # Name is content-hashed (suffix present).
        self.assertRegex(cm["metadata"]["name"],
                         r"^openwebui-workshop-provision-[a-z0-9]{8,}$")
        self.assertEqual(set(cm["data"].keys()),
                         {"provision_openwebui.py", "configuration-models.json"})

    def test_cm_content_equals_git_tracked_files(self):
        cm = [d for d in self.base if d["kind"] == "ConfigMap"
              and d["metadata"]["name"].startswith("openwebui-workshop-provision")][0]
        with open(os.path.join(PROVISIONING, "provision_openwebui.py"),
                  encoding="utf-8") as handle:
            self.assertEqual(cm["data"]["provision_openwebui.py"], handle.read())
        with open(os.path.join(PROVISIONING, "configuration-models.json"),
                  encoding="utf-8") as handle:
            self.assertEqual(cm["data"]["configuration-models.json"], handle.read())

    def test_job_references_hash_rewritten_cm_name(self):
        job = self.find("Job", "openwebui-workshop-provision")
        ref = job["spec"]["template"]["spec"]["volumes"][0]["configMap"]["name"]
        cm_name = [d for d in self.base if d["kind"] == "ConfigMap"
                   and d["metadata"]["name"].startswith(
                       "openwebui-workshop-provision")][0]["metadata"]["name"]
        self.assertEqual(ref, cm_name,
                         "Job volume must reference the hash-rewritten CM name")

    def test_input_change_rolls_hash_and_job_reference(self):
        # A changed sole input must change the generated CM hash and the Job's
        # rewritten volume reference (Job spec roll). Verified in a sandbox
        # copy of the module so the Git tree is never touched.
        with tempfile.TemporaryDirectory() as tmp:
            module_copy = os.path.join(tmp, "module-hsworkshop")
            shutil.copytree(MODULE, module_copy,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            base_copy = os.path.join(module_copy, "install")
            before = build(base_copy)
            presets = os.path.join(module_copy, "provisioning",
                                   "configuration-models.json")
            with open(presets, encoding="utf-8") as handle:
                data = yaml.safe_load(handle)
            data[0]["name"] = data[0]["name"] + " (changed)"
            with open(presets, "w", encoding="utf-8") as handle:
                yaml.safe_dump(data, handle)
            after = build(base_copy)

            def cm_name(docs):
                return [d for d in docs if d["kind"] == "ConfigMap"
                        and d["metadata"]["name"].startswith(
                            "openwebui-workshop-provision")][0]["metadata"]["name"]

            def job_ref(docs):
                job = [d for d in docs if d["kind"] == "Job"
                       and d["metadata"]["name"] == "openwebui-workshop-provision"][0]
                return job["spec"]["template"]["spec"]["volumes"][0]["configMap"]["name"]

            self.assertNotEqual(cm_name(before), cm_name(after),
                                "CM hash must change when the presets input changes")
            self.assertNotEqual(job_ref(before), job_ref(after),
                                "Job volume reference must be rewritten on input change")
            self.assertEqual(job_ref(after), cm_name(after))


class TestPreservationAndOverlay(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = build(BASE)
        cls.overlay = build(OVERLAY)
        cls.base_keys = {key(d) for d in cls.base}

    def test_pvc_and_model_sources_preserved(self):
        # PVCs must exist unchanged in the base.
        pvcs = {d["metadata"]["name"] for d in self.base if d["kind"] == "PersistentVolumeClaim"}
        self.assertIn("openwebui-data", pvcs)
        self.assertIn("pipelines-data", pvcs)
        # OpenWebUI deployment still mounts its data PVC.
        dep = [d for d in self.base if d["kind"] == "Deployment"
               and d["metadata"]["name"] == "openwebui"][0]
        mounts = dep["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]
        self.assertTrue(any(m["name"] == "data" for m in mounts))
        self.assertIn("persistentVolumeClaim",
                      str(dep["spec"]["template"]["spec"]["volumes"]))

    def test_plural_urls_and_signup_false_in_base(self):
        dep = [d for d in self.base if d["kind"] == "Deployment"
               and d["metadata"]["name"] == "openwebui"][0]
        env = env_map(dep)
        self.assertEqual(
            env["OPENAI_API_BASE_URLS"]["value"],
            "http://guardrails-proxy.hsworkshop.svc.cluster.local:8080/v1;"
            "http://pipelines.hsworkshop.svc.cluster.local:9099")
        # No competing singular form.
        self.assertNotIn("OPENAI_API_BASE_URL", env)
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertEqual(env["ENABLE_SIGNUP"]["value"], "false")
        self.assertEqual(env["ENABLE_PERSISTENT_CONFIG"]["value"], "false")

    def test_web_search_preserved_off_and_code_tools_on_in_base(self):
        # Effective values from the owner-admin inventory (docs §13), declared
        # with the env names OpenWebUI 0.12.5 actually reads.
        dep = [d for d in self.base if d["kind"] == "Deployment"
               and d["metadata"]["name"] == "openwebui"][0]
        env = env_map(dep)
        self.assertEqual(env["ENABLE_WEB_SEARCH"]["value"], "false")
        self.assertEqual(env["WEB_SEARCH_ENGINE"]["value"], "searxng")
        self.assertEqual(
            env["SEARXNG_QUERY_URL"]["value"],
            "http://searxng:8080/search?q=<query>&format=json")
        # The legacy 0.11-era names are not read by the pinned 0.12.5 image.
        self.assertNotIn("ENABLE_RAG_WEB_SEARCH", env)
        self.assertNotIn("RAG_WEB_SEARCH_ENGINE", env)
        self.assertEqual(env["ENABLE_CODE_EXECUTION"]["value"], "true")
        self.assertEqual(env["CODE_EXECUTION_ENGINE"]["value"], "pyodide")
        self.assertEqual(env["ENABLE_CODE_INTERPRETER"]["value"], "true")
        self.assertEqual(env["CODE_INTERPRETER_ENGINE"]["value"], "pyodide")

    def test_overlay_preserves_web_search_and_code_tool_settings(self):
        objs = {key(d): d for d in self.overlay}
        dep = objs[("apps/v1", "Deployment", "hsworkshop", "openwebui")]
        env = env_map(dep)
        self.assertEqual(env["ENABLE_WEB_SEARCH"]["value"], "false")
        self.assertEqual(env["WEB_SEARCH_ENGINE"]["value"], "searxng")
        self.assertEqual(env["SEARXNG_QUERY_URL"]["value"],
                         "http://searxng:8080/search?q=<query>&format=json")
        self.assertEqual(env["ENABLE_CODE_EXECUTION"]["value"], "true")
        self.assertEqual(env["CODE_EXECUTION_ENGINE"]["value"], "pyodide")
        self.assertEqual(env["ENABLE_CODE_INTERPRETER"]["value"], "true")
        self.assertEqual(env["CODE_INTERPRETER_ENGINE"]["value"], "pyodide")

    def test_overlay_adds_exactly_five_custom_domain_objects(self):
        objs = {key(d): d for d in self.overlay}
        expected = {
            ("route.openshift.io/v1", "Route", "hsworkshop", "openwebui-custom"),
            ("cert-manager.io/v1", "Issuer", "hsworkshop",
             "letsencrypt-production-dns01"),
            ("cert-manager.io/v1", "Certificate", "hsworkshop",
             "openwebui-custom-cert"),
            ("rbac.authorization.k8s.io/v1", "Role", "hsworkshop",
             "openwebui-custom-cert-reader"),
            ("rbac.authorization.k8s.io/v1", "RoleBinding", "hsworkshop",
             "openwebui-custom-cert-reader"),
        }
        added = set(objs) - self.base_keys
        self.assertEqual(added, expected,
                         f"unexpected overlay additions: {sorted(added)}")
        self.assertFalse({k for k in added if k[1] == "Secret"},
                         "overlay must not add Secrets")

    def test_overlay_preserves_base_objects_and_patches_only_openwebui_env(self):
        objs = {key(d): d for d in self.overlay}
        # Original Route untouched.
        orig = objs[("route.openshift.io/v1", "Route", "hsworkshop", "openwebui")]
        self.assertEqual(orig["spec"]["host"],
                         "openwebui-hsworkshop.apps.kpicl2-pool-rft8p.aws.rh-ods.com")
        self.assertNotIn("externalCertificate", orig["spec"].get("tls", {}))
        # TLS termination preserved on the original Route.
        self.assertEqual(orig["spec"]["tls"]["termination"], "edge")
        # Base openwebui env semantics preserved in the overlay (plural URLs,
        # signup false), with the overlay's custom-domain additions applied.
        dep = objs[("apps/v1", "Deployment", "hsworkshop", "openwebui")]
        env = env_map(dep)
        self.assertEqual(env["WEBUI_URL"]["value"], "https://chat.hsworkshop.fous.ai")
        self.assertTrue(env["OPENAI_API_BASE_URLS"]["value"].startswith(
            "http://guardrails-proxy"), env["OPENAI_API_BASE_URLS"]["value"])
        self.assertEqual(env["ENABLE_SIGNUP"]["value"], "false")
        # The provisioning Job and its hash CM flow through the overlay
        # unchanged (same name/hash as the base).
        base_cm = [d for d in self.base if d["kind"] == "ConfigMap"
                   and d["metadata"]["name"].startswith(
                       "openwebui-workshop-provision")][0]["metadata"]["name"]
        self.assertIn(("v1", "ConfigMap", "hsworkshop", base_cm),
                      set(objs), "provisioning CM missing from overlay")
        overlay_job = objs[("batch/v1", "Job", "hsworkshop",
                            "openwebui-workshop-provision")]
        base_job = [d for d in self.base if d["kind"] == "Job"
                    and d["metadata"]["name"] == "openwebui-workshop-provision"][0]
        self.assertEqual(overlay_job, base_job,
                         "provisioning Job must be identical in base and overlay")

    def test_certificate_only_builds_exactly_two_docs(self):
        docs = build(CERT_ONLY)
        kinds = sorted(d["kind"] for d in docs)
        self.assertEqual(kinds, ["Certificate", "Issuer"])

    def test_base_independence_no_overlay_objects_in_base(self):
        # Semantic independence (NOT whole-base-equals-HEAD): the base build
        # must contain none of the overlay's custom-domain objects.
        names = {key(d) for d in self.base}
        for banned in ("openwebui-custom", "letsencrypt-production-dns01",
                       "openwebui-custom-cert", "openwebui-custom-cert-reader"):
            self.assertFalse(any(banned in n[3] for n in names),
                             f"overlay object {banned} leaked into base")

    def test_no_tls_or_route_removal_in_overlay(self):
        objs = {key(d): d for d in self.overlay}
        # Original Route still present in the overlay build (both origins,
        # ADR-0001 canonical chat preserved).
        self.assertIn(("route.openshift.io/v1", "Route", "hsworkshop", "openwebui"),
                      objs)
        custom = objs[("route.openshift.io/v1", "Route", "hsworkshop",
                       "openwebui-custom")]
        self.assertEqual(custom["spec"]["tls"]["externalCertificate"]["name"],
                         "openwebui-custom-tls")
        self.assertEqual(custom["spec"]["tls"]["termination"], "edge")

    def test_no_plaintext_secrets_in_renders(self):
        for docs in (self.base, self.overlay):
            for d in docs:
                if d.get("kind") == "Secret":
                    self.fail(f"Secret object defined in render: "
                              f"{d['metadata'].get('name')}")
                text = yaml.safe_dump(d)
                for banned in ("BEGIN PRIVATE KEY", "BEGIN CERTIFICATE"):
                    self.assertNotIn(banned, text)


class TestKustomizationWiring(unittest.TestCase):
    def test_install_lists_job_and_nested_provisioning(self):
        with open(os.path.join(BASE, "kustomization.yaml"), encoding="utf-8") as handle:
            k = yaml.safe_load(handle)
        res = k["resources"]
        self.assertIn("openwebui-provisioning.yaml", res)
        self.assertIn("../provisioning", res)
        self.assertIn("../pipelines", res)

    def test_provisioning_kustomization_local_files_only(self):
        with open(os.path.join(PROVISIONING, "kustomization.yaml"),
                  encoding="utf-8") as handle:
            k = yaml.safe_load(handle)
        gens = k["configMapGenerator"]
        self.assertEqual(len(gens), 1)
        self.assertEqual(gens[0]["name"], "openwebui-workshop-provision")
        self.assertEqual(gens[0]["namespace"], "hsworkshop")
        for f in gens[0]["files"]:
            self.assertTrue(os.path.isfile(os.path.join(PROVISIONING, f)),
                            f"generator file {f} must be local to provisioning/")
        self.assertNotIn("resources", k,
                         "generator root must stay resource-free (no cycles)")

    def test_sole_presets_input_no_duplicates(self):
        # Exactly one canonical presets JSON in the whole module.
        found = []
        for root, _dirs, files in os.walk(MODULE):
            if "__pycache__" in root:
                continue
            if "configuration-models.json" in files:
                found.append(os.path.join(root, "configuration-models.json"))
        self.assertEqual(found, [os.path.join(PROVISIONING,
                                              "configuration-models.json")],
                         f"duplicate or misplaced presets JSON: {found}")


if __name__ == "__main__":
    unittest.main()
