"""Research graph persistence and authority boundaries."""
import hashlib
import pytest

from rsi_system.core.runtime_store import RuntimeStore
from rsi_system.core.runtime_research import research_root_id
from rsi_system.core.runtime_communications import snapshot_communications, publish_communications
from rsi_system.core.runtime_types import MissionSpec


def operation(store, task, action, **data):
    return store.apply_research_operation(task["mission_id"], action, data, actor_task_id=task["task_id"])


def coordinator(store, mission_id="m"):
    store.create_mission(MissionSpec(mission_id, "Investigate a mechanism"))
    store.create_task(mission_id, "coordinator", "plan", "research")
    return store.claim_task(mission_id)


def create(store, actor, node_id, *, parent=None):
    return operation(store, actor, "create", node_id=node_id, question=f"Question {node_id}",
                     scope="Investigate this question", reason="Separate uncertainty", parent=parent)["node"]


def test_old_revision_cross_depth_composition_and_history(tmp_path):
    path = tmp_path / "runtime.sqlite"
    with RuntimeStore(path) as store:
        actor = coordinator(store)
        root = create(store, actor, "root")
        a = create(store, actor, "a", parent={"node_id": "root", "revision": 1})
        b = create(store, actor, "b", parent={"node_id": "root", "revision": 1})
        a1 = create(store, actor, "a1", parent={"node_id": "a", "revision": 1})
        old = operation(store, actor, "revise", node_id="a1", expected_revision=1,
                        summary="Early result", reason="Found preliminary evidence")["node"]
        old_cursor = store.mission("m")["version"]
        operation(store, actor, "revise", node_id="a1", expected_revision=2,
                  summary="Later result", reason="Additional evidence")
        relation = operation(store, actor, "relate", kind="complements",
                             source={"node_id": "a1", "revision": 2}, target={"node_id": "b", "revision": 1},
                             reason="Possible interaction")["relation"]
        assert relation["status"] == "proposed" and relation["kind"] == "complements"
        joint = operation(store, actor, "compose", node_id="joint", question="Do these results combine?",
                          scope="Test a joint mechanism", reason="Potential complementarity",
                          inputs=[{"node_id": "a1", "revision": 2}, {"node_id": "b", "revision": 1}])
        assert joint["node"]["finding_status"] == "unknown"
        assert [item["revision"] for item in joint["composition"]["inputs"]] == [2, 1]
        assert {r["kind"] for r in joint["relations"]} == {"composed_from"}
        assert store.research_node("m", "a1", 2)["summary"] == "Early result"
        assert store.research_node("m", "a1")["summary"] == "Later result"
        assert {n["node_id"] for n in store.research_snapshot("m")["nodes"]} == {"root", "a", "b", "a1", "joint"}
        historical = store.research_snapshot("m", at_version=old_cursor)
        assert {n["node_id"] for n in historical["nodes"]} == {"root", "a", "b", "a1"}
        assert next(n for n in historical["nodes"] if n["node_id"] == "a1")["revision"] == 2
        assert historical["compositions"] == []
        assert historical["relations"] and all(r["kind"] == "decomposes" for r in historical["relations"])
    with RuntimeStore.open_readonly(path) as store:
        assert store.research_snapshot("m")["compositions"][0]["inputs"][0]["revision"] == old["revision"]


def test_composition_contract_and_append_only_revocation(tmp_path):
    with RuntimeStore(tmp_path / "runtime.sqlite") as store:
        actor = coordinator(store)
        create(store, actor, "a")
        create(store, actor, "b")
        composed = operation(store, actor, "compose", node_id="joint", question="Can they interact?",
                             scope="joint", reason="test a concrete interaction", inputs=[
                                 {"node_id": "a", "revision": 1, "selection": "summary"},
                                 {"node_id": "b", "revision": 1, "selection": "mechanism"}],
                             interaction_assumptions=["A changes the operating regime of B"],
                             evaluation_protocol={"protocol_id": "joint-eval-v1", "version": "1"},
                             lineage={"decision": "complementarity hypothesis"})
        contract = composed["composition"]["plan"]["contract"]
        assert composed["composition"]["contract"] == contract
        assert contract["inputs"] == composed["composition"]["inputs"]
        assert contract["interaction_assumptions"] == ["A changes the operating regime of B"]
        assert contract["evaluation_protocol"]["protocol_id"] == "joint-eval-v1"
        assert contract["output_node_revision"] == 1
        assert contract["lineage"]["source_revisions"] == [
            {"node_id": "a", "revision": 1}, {"node_id": "b", "revision": 1}]
        assert contract["lineage"]["experimental_support"] == "not_established"
        assert composed["node"]["revision"] == 1
        before = store.mission("m")["version"]
        revoked = operation(store, actor, "revoke_composition",
                            composition_id=composed["composition"]["composition_id"], reason="source was withdrawn")
        assert revoked["composition"]["status"] == "revoked"
        assert revoked["composition"]["lifecycle_revision"] == 2
        assert store.research_node("m", "joint", 1)["composition_contract"] == contract
        lifecycle = store.research_snapshot("m")["compositions"][0]["lifecycle"]
        assert [item["status"] for item in lifecycle] == ["active", "revoked"]
        assert store.mission("m")["version"] == before + 1
        with pytest.raises(ValueError, match="already revoked"):
            operation(store, actor, "rollback", composition_id=composed["composition"]["composition_id"],
                      reason="duplicate revoke")


def test_composition_contract_rejects_malformed_protocol_atomically(tmp_path):
    with RuntimeStore(tmp_path / "runtime.sqlite") as store:
        actor = coordinator(store)
        create(store, actor, "a")
        create(store, actor, "b")
        before = store.mission("m")["version"]
        with pytest.raises(ValueError, match="protocol_id"):
            operation(store, actor, "compose", node_id="bad", question="bad", scope="bad",
                      reason="bad", inputs=[{"node_id": "a", "revision": 1},
                                             {"node_id": "b", "revision": 1}],
                      interaction_assumptions=["uncertain"], evaluation_protocol={"version": 1})
        assert store.mission("m")["version"] == before
        assert store.research_node("m", "bad") is None


def test_scope_binding_and_cross_branch_authority(tmp_path):
    with RuntimeStore(tmp_path / "runtime.sqlite") as store:
        actor = coordinator(store)
        create(store, actor, "root")
        create(store, actor, "a", parent={"node_id": "root", "revision": 1})
        create(store, actor, "b", parent={"node_id": "root", "revision": 1})
        assigned = store.create_task("m", "researcher", "research a", "assigned",
                                     research_node_id="a", research_revision=1)
        worker = store.claim_task("m")
        assert worker["task_id"] == assigned["task_id"]
        assert worker["scope"]["research_node_id"] == "a"
        create(store, worker, "a1", parent={"node_id": "a", "revision": 1})
        child = store.create_task("m", "coder", "implement", "follow up", parent_task_id=worker["task_id"])
        assert child["scope"]["research_node_id"] == "a"
        assert child["scope"]["research_revision"] == 1
        with pytest.raises(ValueError, match="assigned branch"):
            operation(store, worker, "revise", node_id="b", expected_revision=1, reason="cross branch",
                      summary="Cannot edit")
        with pytest.raises(ValueError, match="assigned branch"):
            operation(store, worker, "compose", node_id="joint", question="combine?", scope="study",
                      reason="cross branch", inputs=[{"node_id": "a1", "revision": 1},
                                                     {"node_id": "b", "revision": 1}])
        with pytest.raises(ValueError, match="subtree"):
            store.create_task("m", "coder", "wrong", "wrong", parent_task_id=worker["task_id"],
                              research_node_id="b", research_revision=1)
        assert store.research_node("m", "joint") is None


def test_local_joint_task_can_follow_scoped_composition(tmp_path):
    with RuntimeStore(tmp_path / "runtime.sqlite") as store:
        actor = coordinator(store)
        create(store, actor, "root")
        create(store, actor, "a", parent={"node_id": "root", "revision": 1})
        create(store, actor, "a1", parent={"node_id": "a", "revision": 1})
        create(store, actor, "a2", parent={"node_id": "a", "revision": 1})
        store.create_task("m", "researcher", "local", "assigned", idea_id="a",
                          research_node_id="a", research_revision=1)
        worker = store.claim_task("m")
        joint = operation(store, worker, "compose", node_id="local-joint", question="Combine?",
                          scope="Local study", reason="Two related results",
                          inputs=[{"node_id": "a1", "revision": 1}, {"node_id": "a2", "revision": 1}])["node"]
        task = store.create_task("m", "researcher", "Combine?", "follow up", idea_id="local-joint",
                                 parent_task_id=worker["task_id"], research_node_id=joint["node_id"],
                                 research_revision=joint["revision"])
        assert task["scope"]["research_node_id"] == "local-joint"
        assert task["scope"]["idea_id"] == "local-joint"


def test_invalid_references_cycles_and_atomic_rollback(tmp_path):
    with RuntimeStore(tmp_path / "runtime.sqlite") as store:
        actor = coordinator(store)
        create(store, actor, "a")
        create(store, actor, "b")
        before = store.mission("m")["version"]
        with pytest.raises(ValueError, match="existing revision"):
            operation(store, actor, "compose", node_id="bad", question="bad", scope="bad", reason="bad",
                      inputs=[{"node_id": "a", "revision": 1}, {"node_id": "b", "revision": 2}])
        assert store.mission("m")["version"] == before
        assert store.research_snapshot("m")["cursor"] == before
        assert store.research_node("m", "bad") is None
        with pytest.raises(ValueError, match="conflict"):
            operation(store, actor, "revise", node_id="a", expected_revision=3, reason="stale", summary="bad")
        operation(store, actor, "relate", kind="depends_on", source={"node_id": "a", "revision": 1},
                  target={"node_id": "b", "revision": 1}, reason="a feeds b")
        with pytest.raises(ValueError, match="cycle"):
            operation(store, actor, "relate", kind="depends_on", source={"node_id": "b", "revision": 1},
                      target={"node_id": "a", "revision": 1}, reason="b feeds a")
        with pytest.raises(ValueError, match="lineage"):
            operation(store, actor, "relate", kind="decomposes", source={"node_id": "a", "revision": 1},
                      target={"node_id": "b", "revision": 1}, reason="fabrication")
        with pytest.raises(ValueError, match="actor task"):
            store.apply_research_operation("m", "create", {"node_id": "no", "question": "x", "scope": "x",
                                             "reason": "x"}, actor_task_id=None)
        store.create_mission(MissionSpec("other", "Another"))
        store.apply_research_operation("other", "create", {"node_id": "other-root", "question": "Other",
            "scope": "Other Mission", "reason": "Start"})
        with pytest.raises(ValueError, match="this Mission"):
            operation(store, actor, "compose", node_id="cross", question="cross", scope="cross", reason="cross",
                      inputs=[{"node_id": "a", "revision": 1}, {"node_id": "other-root", "revision": 1}])


def test_outer_transaction_rolls_back_graph_and_task(tmp_path):
    with RuntimeStore(tmp_path / "runtime.sqlite") as store:
        actor = coordinator(store)
        create(store, actor, "a")
        create(store, actor, "b")
        cursor = store.mission("m")["version"]
        with pytest.raises(RuntimeError, match="dispatch failed"):
            with store.atomic():
                joint = operation(store, actor, "compose", node_id="joint", question="Joint?", scope="Joint",
                    reason="Try both", inputs=[{"node_id": "a", "revision": 1},
                                               {"node_id": "b", "revision": 1}])
                store.create_task("m", "researcher", "Joint?", "follow up", parent_task_id=actor["task_id"],
                                  research_node_id=joint["node"]["node_id"], research_revision=1)
                raise RuntimeError("dispatch failed")
        assert store.mission("m")["version"] == cursor
        assert store.research_node("m", "joint") is None
        assert store.research_snapshot("m")["compositions"] == []
        assert len(store.list_tasks("m")) == 1


def test_research_migration_preserves_legacy_tasks_and_no_synthetic_nodes(tmp_path):
    path = tmp_path / "runtime.sqlite"
    with RuntimeStore(path) as store:
        store.create_mission(MissionSpec("m", "Historical chat"))
        task = store.create_task("m", "researcher", "old task", "legacy")
        assert task["scope"]["research_node_id"] is None
        for table in ("research_task_bindings", "research_compositions", "research_relation_revisions",
                      "research_relations", "research_node_revisions", "research_nodes"):
            store.db.execute(f"DROP TABLE {table}")
        store.db.commit()
    with RuntimeStore(path) as store:
        assert store.research_snapshot("m")["nodes"] == []
        assert store.task(task["task_id"])["scope"]["research_node_id"] is None
        assert store.research_snapshot("m")["task_bindings"] == []


def test_legacy_root_fallback_and_explicit_separate_root(tmp_path):
    with RuntimeStore(tmp_path / "runtime.sqlite") as store:
        actor = coordinator(store)
        create(store, actor, "historical-root")
        child = create(store, actor, "child")
        edge = next(edge for edge in store.research_snapshot("m")["relations"]
                    if edge["target"]["node_id"] == child["node_id"])
        assert edge["source"] == {"node_id": "historical-root", "revision": 1}
        another = create(store, actor, "another-child")
        assert any(edge["source"]["node_id"] == "historical-root" and
                   edge["target"]["node_id"] == another["node_id"]
                   for edge in store.research_snapshot("m")["relations"])
        operation(store, actor, "create", node_id="independent", question="Independent question",
                  scope="parallel scope", reason="distinct root", separate_root=True)
        with pytest.raises(ValueError, match="ambiguous"):
            create(store, actor, "unspecified")
        with pytest.raises(ValueError, match="cannot be combined"):
            operation(store, actor, "create", node_id="conflicting", question="Conflict",
                      scope="conflict", reason="invalid", separate_root=True,
                      parent={"node_id": "historical-root", "revision": 1})
        with pytest.raises(ValueError, match="boolean"):
            operation(store, actor, "create", node_id="bad-flag", question="Bad flag",
                      scope="invalid", reason="invalid", separate_root="true")
        assert store.research_node("m", "unspecified") is None
        assert store.research_node("m", "conflicting") is None
        assert store.research_node("m", "bad-flag") is None


def test_bootstrap_root_and_relation_lifecycle(tmp_path):
    with RuntimeStore(tmp_path / "runtime.sqlite") as store:
        store.create_mission(MissionSpec("m", "Initial question"), status="paused")
        root = store.apply_research_operation("m", "create", {"node_id": "m-root", "question": "Initial question",
            "scope": "Mission", "reason": "Mission start"})["node"]
        assert root["input_refs"] == []
        with pytest.raises(ValueError, match="actor task"):
            store.apply_research_operation("m", "create", {"node_id": "unowned", "question": "No",
                "scope": "No", "reason": "No actor"})
        store.set_mission_status("m", "active", reason="begin")
        store.create_task("m", "coordinator", "plan", "research", research_node_id="m-root")
        actor = store.claim_task("m")
        create(store, actor, "branch", parent={"node_id": "m-root", "revision": 1})
        edge = operation(store, actor, "relate", kind="complements", source={"node_id": "m-root", "revision": 1},
            target={"node_id": "branch", "revision": 1}, reason="May interact")["relation"]
        assert edge["status"] == "proposed"
        accepted = operation(store, actor, "relate", relation_id=edge["relation_id"], expected_revision=1,
            reason="Worth examining", status="accepted")["relation"]
        assert accepted["revision"] == 2 and accepted["status"] == "accepted"
        with pytest.raises(ValueError, match="revision conflict"):
            operation(store, actor, "relate", relation_id=edge["relation_id"], expected_revision=True,
                reason="Boolean is not a revision", status="withdrawn")
        with pytest.raises(ValueError, match="requires evidence"):
            operation(store, actor, "relate", relation_id=edge["relation_id"], expected_revision=2,
                reason="Unsupported claim", status="supported")
        with pytest.raises(ValueError, match="Run is absent"):
            operation(store, actor, "relate", relation_id=edge["relation_id"], expected_revision=2,
                reason="Forged reference", status="supported", evidence_refs=["run:missing"])
        run = store.create_run("m", None, actor["task_id"], "exploration", "completed", None,
                               {"question": "Do the findings interact?"}, {"observed": True}, idea_ref="idea-a")
        supported = operation(store, actor, "relate", relation_id=edge["relation_id"], expected_revision=2,
            reason="A recorded observation supports the link", status="supported",
            evidence_refs=[f"run:{run['run_id']}"])["relation"]
        assert supported["status"] == "supported"
        assert supported["status_basis"] == "actor_judgment"
        assert store.research_node("m", "branch")["finding_status"] == "unknown"
        historical = store.research_snapshot("m", at_version=edge["event_version"])
        assert next(r for r in historical["relations"] if r["relation_id"] == edge["relation_id"])["status"] == "proposed"
        assert store.research_snapshot("m")["task_bindings"][0]["node_id"] == "m-root"


def test_finding_evidence_resolves_same_mission_run_or_published_handoff(tmp_path):
    path = tmp_path / "runtime.sqlite"
    with RuntimeStore(path) as store:
        actor = coordinator(store)
        node = create(store, actor, "finding")
        with pytest.raises(ValueError, match="requires evidence"):
            operation(store, actor, "revise", node_id="finding", expected_revision=1,
                      reason="No evidence", finding_status="mixed")
        with pytest.raises(ValueError, match="not in this Mission"):
            operation(store, actor, "revise", node_id="finding", expected_revision=1,
                      reason="Forged evidence", finding_status="supported", run_refs=["missing"])
        store.create_mission(MissionSpec("other", "Another Mission"))
        store.create_task("other", "coordinator", "plan", "other")
        other_actor = store.claim_task("other")
        foreign = store.create_run("other", None, other_actor["task_id"], "exploration", "completed",
                                   None, {}, {"observed": True}, idea_ref="foreign")
        with pytest.raises(ValueError, match="not in this Mission"):
            operation(store, actor, "revise", node_id="finding", expected_revision=1,
                      reason="Foreign evidence", finding_status="refuted", run_refs=[foreign["run_id"]])
        run = store.create_run("m", None, actor["task_id"], "exploration", "failed", None,
                               {"test": "falsification"}, {"observed": "negative"}, idea_ref="local")
        assessed = operation(store, actor, "revise", node_id="finding", expected_revision=1,
                             reason="Negative observation", finding_status="refuted",
                             run_refs=[run["run_id"]])["node"]
        assert assessed["finding_status_basis"] == "actor_judgment"
        assert assessed["run_refs"] == [run["run_id"]]
        output_dir = path.parent / "tasks" / actor["task_id"]
        manifest = snapshot_communications(actor, output_dir, {"summary": "published account"})
        publish_communications(store, actor, manifest, "completed")
        handoff_ref = f"handoff:{actor['task_id']}:{hashlib.sha256(manifest.read_bytes()).hexdigest()}"
        handoff = operation(store, actor, "revise", node_id="finding", expected_revision=2,
                            reason="Published account", finding_status="mixed", run_refs=[],
                            artifact_refs=[handoff_ref])["node"]
        assert handoff["finding_status"] == "mixed" and handoff["artifact_refs"] == [handoff_ref]
        manifest.write_text("changed")
        with pytest.raises(ValueError, match="missing or changed"):
            operation(store, actor, "revise", node_id="finding", expected_revision=3,
                      reason="Stale handoff", finding_status="supported")


def test_long_mission_root_id_is_safe_and_deterministic(tmp_path):
    mission_id = "m" * 96
    root_id = research_root_id(mission_id)
    assert root_id == research_root_id(mission_id) and len(root_id) <= 96
    assert research_root_id("ordinary") == "ordinary-root"
    with RuntimeStore(tmp_path / "runtime.sqlite") as store:
        store.create_mission(MissionSpec(mission_id, "A long Mission identifier"))
        node = store.apply_research_operation(mission_id, "create", {"node_id": root_id,
            "question": "A long Mission identifier", "scope": "Mission", "reason": "Start"})["node"]
        assert node["node_id"] == root_id
        with pytest.raises(ValueError, match="actor task"):
            store.apply_research_operation(mission_id, "create", {"node_id": "another",
                "question": "No", "scope": "No", "reason": "No actor"})
