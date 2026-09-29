"""Identify research feedback separately from task and publication bookkeeping."""

REPORT_EVENT_KINDS = frozenset({
    "analyzer_report", "critic_report", "exploration_analyzer_report", "exploration_critic_report",
    "research_analyzer_report", "research_critic_report",
})
REVIEW_EVENT_KINDS = frozenset({
    "research_decision", "research_run_link", "problem_reframe_report", "analyzer_report", "critic_report",
    "exploration_analyzer_report", "exploration_critic_report",
})
FINAL_REVIEW_EVENT_KINDS = REVIEW_EVENT_KINDS | REPORT_EVENT_KINDS


def new_research_feedback(store, mission_id, after_version, *, final_review=False):
    """Find durable feedback needing review, including legacy unbound tasks."""
    found = []
    for event in store.events(mission_id):
        if event["version"] <= after_version:
            continue
        payload = event["payload"]
        if event["kind"] in (FINAL_REVIEW_EVENT_KINDS if final_review else REVIEW_EVENT_KINDS):
            if not final_review and event["kind"] in {"research_decision", "research_run_link"}:
                source = store.task(payload.get("task_id")) if payload.get("task_id") else None
                if not source or not source["scope"].get("research_node_id"):
                    continue
            found.append(event)
        elif final_review and event["kind"] in {"run_registered", "exploration_feedback"}:
            found.append(event)
        elif (final_review and event["kind"] == "communication_published"
              and payload.get("author_role") not in {"coordinator", "curator"}
              and payload.get("task_status") == "completed"):
            found.append(event)
    return found
