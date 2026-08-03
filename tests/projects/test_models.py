from __future__ import annotations

from dataclasses import replace

import pytest

from cadscene.projects.models import (
    ClipDefinition,
    ClipsManifest,
    JobsManifest,
    ProjectManifest,
    RenderManifest,
    activate_analysis_revision,
    reconcile_clips,
    register_analysis_revision,
)


def _clip(
    *,
    revision: str,
    generated_name: str,
    recommendation: str | None,
    custom_name: str | None = None,
    override: str | None = None,
) -> ClipDefinition:
    return ClipDefinition.from_analysis(
        {
            "project_id": "p1",
            "clip_id": "clip-permanent",
            "analysis_revision": revision,
            "source_start_pts": 5000,
            "source_end_pts_exclusive": 9000,
            "source_time_base": {"numerator": 1, "denominator": 1000},
            "interval_semantics": "half_open",
            "recommended_workflow": recommendation,
        },
        generated_display_name=generated_name,
        custom_display_name=custom_name,
        workflow_override=override,
    )


def test_each_manifest_has_an_independent_header_and_owner():
    common = {
        "schema_version": "1.0",
        "revision": 3,
        "updated_at": "2026-08-03T00:00:00Z",
        "project_id": "p1",
    }

    manifests = (
        ProjectManifest(**common),
        ClipsManifest(**common),
        JobsManifest(**common),
        RenderManifest(**common),
    )

    assert [manifest.owner for manifest in manifests] == [
        "project",
        "clips",
        "jobs",
        "render",
    ]
    assert all(manifest.revision == 3 for manifest in manifests)


def test_logical_clip_contract_keeps_integer_pts_and_half_open_time_base():
    clip = _clip(
        revision="analysis-1",
        generated_name="Scene 01 - Segment 1",
        recommendation="sfm_only",
    )

    assert clip.clip_id == "clip-permanent"
    assert clip.analysis["source_start_pts"] == 5000
    assert clip.analysis["source_end_pts_exclusive"] == 9000
    assert clip.analysis["source_time_base"] == {"numerator": 1, "denominator": 1000}
    assert clip.analysis["interval_semantics"] == "half_open"


def test_custom_name_and_workflow_override_survive_analysis_refresh():
    user_edited = _clip(
        revision="analysis-1",
        generated_name="Scene 01 - Segment 1",
        recommendation="sfm_only",
        custom_name="East entrance",
        override="pure_rotation",
    )
    user_edited = replace(user_edited, manual_definition={"split_pts": [7100]})
    candidate = _clip(
        revision="analysis-2",
        generated_name="Scene 02 - Segment 1",
        recommendation="srt_full_pose",
    )

    refreshed = reconcile_clips(existing=user_edited, candidate=candidate)

    assert refreshed.generated_display_name == "Scene 02 - Segment 1"
    assert refreshed.custom_display_name == "East entrance"
    assert refreshed.display_name == "East entrance"
    assert refreshed.recommended_workflow == "srt_full_pose"
    assert refreshed.workflow_override == "pure_rotation"
    assert refreshed.resolved_workflow == "pure_rotation"
    assert refreshed.manual_definition == {"split_pts": [7100]}


def test_null_workflow_override_restores_recommendation():
    clip = _clip(
        revision="analysis-1",
        generated_name="Scene 01 - Segment 1",
        recommendation="sfm_only",
        override="pure_rotation",
    )

    restored = clip.with_workflow_override(None)

    assert restored.workflow_override is None
    assert restored.resolved_workflow == "sfm_only"


def test_non_null_custom_name_is_the_resolved_layer_even_when_empty():
    clip = _clip(
        revision="analysis-1",
        generated_name="Generated name",
        recommendation="sfm_only",
        custom_name="",
    )

    assert clip.custom_display_name == ""
    assert clip.display_name == ""


def test_initial_analysis_activates_but_later_analysis_is_only_a_candidate():
    project = ProjectManifest.new("p1", updated_at="2026-08-03T00:00:00Z")

    initially_analyzed = register_analysis_revision(
        project, "analysis-1", operation_id="op-initial"
    )
    reanalyzed = register_analysis_revision(
        initially_analyzed, "analysis-2", operation_id="op-candidate"
    )

    assert initially_analyzed.active_analysis_revision == "analysis-1"
    assert initially_analyzed.candidate_analysis_revision is None
    assert reanalyzed.active_analysis_revision == "analysis-1"
    assert reanalyzed.candidate_analysis_revision == "analysis-2"
    assert reanalyzed.active_analysis_operation_id == "op-initial"
    assert reanalyzed.candidate_analysis_operation_id == "op-candidate"
    assert reanalyzed.analysis_operation_ids == {
        "analysis-1": "op-initial",
        "analysis-2": "op-candidate",
    }


def test_register_analysis_revision_is_idempotent_for_the_same_operation():
    project = ProjectManifest.new("p1", updated_at="2026-08-03T00:00:00Z")
    registered = register_analysis_revision(
        project, "analysis-1", operation_id="op-analysis-1"
    )

    repeated = register_analysis_revision(
        registered, "analysis-1", operation_id="op-analysis-1"
    )

    assert repeated == registered


def test_register_analysis_revision_rejects_operation_rebinding():
    project = ProjectManifest.new("p1", updated_at="2026-08-03T00:00:00Z")
    registered = register_analysis_revision(
        project, "analysis-1", operation_id="op-analysis-1"
    )

    with pytest.raises(ValueError, match="immutable analysis revision"):
        register_analysis_revision(
            registered, "analysis-1", operation_id="different-operation"
        )


def test_project_manifest_rejects_divergent_immutable_analysis_ownership():
    payload = ProjectManifest.new(
        "p1", updated_at="2026-08-03T00:00:00Z"
    ).to_dict()
    payload["analysis_revisions"] = ["analysis-orphan"]

    with pytest.raises(ValueError, match="analysis_operation_ids"):
        ProjectManifest.from_dict(payload)


def test_job_and_render_states_require_stable_identity():
    common = {
        "schema_version": "1.0",
        "revision": 0,
        "updated_at": "2026-08-03T00:00:00Z",
        "project_id": "p1",
    }

    with pytest.raises(ValueError, match="job_id"):
        JobsManifest(**common, jobs=({"status": "queued"},))
    with pytest.raises(ValueError, match="output_id"):
        RenderManifest(**common, published_outputs=({"status": "planned"},))


def test_explicit_candidate_activation_preserves_user_layers():
    project = ProjectManifest.new("p1", updated_at="2026-08-03T00:00:00Z")
    project = register_analysis_revision(project, "analysis-1", operation_id="op-1")
    project = register_analysis_revision(project, "analysis-2", operation_id="op-2")
    current = ClipsManifest.new(
        "p1",
        analysis_revision="analysis-1",
        clips=(
            _clip(
                revision="analysis-1",
                generated_name="Old generated name",
                recommendation="sfm_only",
                custom_name="Permanent user name",
                override="pure_rotation",
            ),
        ),
        updated_at="2026-08-03T00:00:00Z",
    )
    candidate = ClipsManifest.new(
        "p1",
        analysis_revision="analysis-2",
        clips=(
            _clip(
                revision="analysis-2",
                generated_name="New generated name",
                recommendation="srt_full_pose",
            ),
        ),
        updated_at="2026-08-03T00:00:00Z",
    )

    activated_project, activated_clips = activate_analysis_revision(
        project,
        current,
        candidate,
        operation_id="op-activate",
    )

    assert activated_project.active_analysis_revision == "analysis-2"
    assert activated_project.candidate_analysis_revision is None
    assert activated_project.active_analysis_operation_id == "op-activate"
    assert activated_project.candidate_analysis_operation_id is None
    assert activated_clips.analysis_revision == "analysis-2"
    assert activated_clips.clips[0].custom_display_name == "Permanent user name"
    assert activated_clips.clips[0].workflow_override == "pure_rotation"
    assert activated_project.operation_id == activated_clips.operation_id == "op-activate"
