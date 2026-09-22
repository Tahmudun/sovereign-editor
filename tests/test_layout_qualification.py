"""Focused read-only M5 prerequisite tests; no ROM export or live mutation."""
import copy
import json
import struct
from pathlib import Path

import ndspy.codeCompression
import pytest

from sovereign_editor.core import Project
from sovereign_editor.formats import EditorError, digest, resource
from sovereign_editor.layout_qualification import (
    ORIGINAL_PROPOSAL, PROPOSAL, assess_layout, build_definition, canonical_hash, components,
    compose_cells, connection_check, entry_table, footprint, height_coverage,
    ownership, require_layout, script_audit, terrain,
)
from sovereign_editor.native_layout import (
    actor_reference, initialization, movement_definitions, movement_stream,
    overlay_image, project_event_routes,
)
from sovereign_editor.runtime_contracts import (
    ACTIVE, NORMAL_BUSY, HELD, FINISHED, PAUSED, actor_update_lane,
    held_complete, stream_ready, follower_step, conditional_follower_sweep,
    child_serialization, enqueue_follower_action,
)

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def project():
    return Project(ROOT / "projects/cherrygrove")


@pytest.fixture(scope="module")
def definition(project):
    return build_definition(project)


def plane(index, bounds, height=65536):
    return {"index":index,"bounds_raw":bounds,"height_raw":height}


def height_data(planes, strips=None):
    return {"plates":planes,"strips":strips or [{"upper_raw":1000000,"plates":[p["index"] for p in planes]}]}


def test_closed_tile_crossing_equal_height_plates():
    data = height_data([plane(0,[0,0,32768,65536]),plane(1,[32768,0,65536,65536])])
    result = height_coverage(data,(560,400))
    assert result["qualified"] and result["interior_plates"] == [0,1]


@pytest.mark.parametrize("planes,code", [
    ([plane(0,[0,0,32767,65536]),plane(1,[32768,0,65536,65536])],"HEIGHT_GAP_OVERLAP_OR_LEVEL"),
    ([plane(0,[0,0,40000,65536]),plane(1,[32768,0,65536,65536])],"HEIGHT_GAP_OVERLAP_OR_LEVEL"),
    ([plane(0,[0,0,65536,65536]),plane(1,[65536,0,131072,65536],0)],"HEIGHT_BOUNDARY_AMBIGUITY"),
    ([plane(0,[0,0,65536,65536],0)],"HEIGHT_GAP_OVERLAP_OR_LEVEL"),
])
def test_height_refuses_one_fixed_point_gap_overlap_and_boundary_disagreement(planes,code):
    result = height_coverage(height_data(planes),(560,400))
    assert not result["qualified"] and code in result["refusals"]


def test_strip_tie_requires_both_incident_lists():
    data = height_data([plane(0,[0,-65536,65536,0]),plane(1,[0,0,65536,65536])],
                      [{"upper_raw":0,"plates":[0,1]},{"upper_raw":65536,"plates":[1]}])
    assert height_coverage(data,(560,400))["qualified"]
    data["strips"][0]["plates"] = []
    assert "HEIGHT_STRIP_COVERAGE" in height_coverage(data,(560,400))["refusals"]


def test_native_source_edges_and_two_plate_source(definition):
    src = definition["sources"]
    assert [h["interior_plates"] for h in src["5:12"]["height"]] == [[11],[11],[11]]
    assert src["5:12"]["height"][2]["incident_plates"] == [11,12]
    assert [h["interior_plates"] for h in src["5:13"]["height"]] == [[5],[6],[6]]
    assert all(h["qualified"] for s in src.values() for h in s["height"])
    assert len(definition["anchors"]) == 23
    assert all(a["neutral"] and not a["static_event_conflicts"] and all(h["qualified"] for h in a["height"])
               for a in definition["anchors"])


def test_bdhc_bad_strip_index_refused(project):
    _,raw = resource(project.blob,"a/0/6/5",5)
    data = terrain(raw); indices = next(r for r in data["records"] if r["section"] == "indices")
    changed = bytearray(raw); struct.pack_into("<H",changed,indices["map_offset"],65535)
    with pytest.raises(EditorError,match="strip plate outside"):
        terrain(changed)


def test_native_ownership_controls_and_reserved_ambiguity(definition):
    for key in ("5:12","5:14"):
        s = definition["sources"][key]
        assert s["ownership_qualified"] and len(s["control"]["body_door_pairs"]) == 12
        assert all(a["before"] == b["before"] for a,b in s["control"]["body_door_pairs"])
        assert all(c["clear_allowed"] and c["exclusive_owner"] == key and not c["retained_owners"] for c in s["cells"])
    s = definition["sources"]["5:13"]
    assert not s["ownership_qualified"]
    assert all(not c["clear_allowed"] and c["exclusive_owner"] is None and c["retained_owners"] for c in s["cells"])
    assert s["refusals"][0]["code"] == "OWNERSHIP_AMBIGUOUS"


@pytest.mark.parametrize("part",["body","flank","transform"])
def test_house_control_tampering_refused(project,definition,part):
    maps = {m:resource(project.blob,"a/0/6/5",m)[1] for m in (5,8,296)}
    raw = bytearray(maps[8]); ctrl = definition["sources"]["5:12"]["control"]
    if part == "body":
        offset = ctrl["body_door_pairs"][0][1]["offset"]; raw[offset] ^= 1
    elif part == "flank":
        offset = ctrl["passable_flank"][0]["offset"]; raw[offset+1] |= 128
    else:
        # Slot 1 record X: 20 + 2048 + 48 + 4, plus native extra header.
        offset = 20+struct.unpack_from("<H",raw,18)[0]+2048+48+4
        raw[offset] ^= 1
    maps[8] = bytes(raw)
    with pytest.raises(EditorError):
        ownership(maps)


def test_composition_keeps_shared_owner_and_nonblocking_bits():
    source = {"a":{"cells":[{"cell":[0,0],"clear_allowed":True,"exclusive_owner":"a","retained_owners":[]},
                              {"cell":[0,1],"clear_allowed":True,"exclusive_owner":"a","retained_owners":["house"]}]}}
    before = {(0,0):"0684",(0,1):"0080",(2,1):"0000",(2,2):"0000",(2,3):"0000"}
    result,failures = compose_cells(before,source,{"a":[2.5,2.5]})
    assert result[(0,0)] == "0604" and result[(0,1)] == "0080"
    assert not failures and before[(0,0)] == "0684"


def test_composition_detects_shared_destination_conflicts():
    source = {"a":{"cells":[]},"b":{"cells":[]}}
    before = {(0,z):"0000" for z in range(4)}
    _,failures = compose_cells(before,source,{"a":[.5,1.5],"b":[.5,2.5]})
    assert {tuple(f["cell"]) for f in failures if f["code"] == "DECORATION_OVERLAP"} == {(0,1),(0,2)}


def test_retained_blocker_cannot_be_reused():
    before = {(0,z):"0080" if z == 1 else "0000" for z in range(3)}
    _,failures = compose_cells(before,{"a":{"cells":[]}},{"a":[.5,1.5]})
    assert any(f["code"] == "RETAINED_COLLISION_CONFLICT" for f in failures)


def test_walking_connections_detect_combined_severance():
    before = {(x,z) for x in range(5) for z in range(3)}
    assert connection_check(before,before-{(2,0),(2,1)})["preserved"]
    assert connection_check(before,before-{(2,2)})["preserved"]
    # Separately admissible obstructions jointly sever the corridor.
    result = connection_check(before,before-{(2,0),(2,1),(2,2)})
    assert not result["preserved"] and result["split_witnesses"] == [[[0,0],[3,0]]]


def test_existing_disconnected_components_do_not_require_new_connections():
    before = {(0,0),(0,1),(3,0),(3,1)}
    assert len(components(before)) == 2
    assert connection_check(before,before-{(0,1)})["preserved"]


def test_native_rooted_trace_and_cross_script_dispatch(definition):
    s = definition["scripts"]
    assert len(s["decoded"]) == 625
    by = {(r["member"],r["offset"]):r for r in s["decoded"]}
    assert by[(850,0x89)]["kind"] == "end"
    assert by[(850,0x8b)]["opcode"] == 0x62
    assert by[(850,0x99)]["dispatched_entry"] == {"member":3,"entry_index":37,"offset":0x135c}
    assert {(r["script_id"],r["member"],r["entry_index"],r["offset"]) for r in s["event_dispatch"]
            if r["kind"] == "backgrounds" and r["script_id"] >= 8000} == {(8001,145,1,0x402),(8225,145,225,0x402)}
    assert len(s['movement_references']) == 55 and len(s['movement_streams']) == 47
    first = next(r for r in s['movement_references'] if r['command_offset'] == 0x8f)
    assert first['stream_offset'] == 0x350 and first['actor_reference']['id'] == 0
    assert by[(3,0x135c)]['opcode'] == 0x51 and by[(3,0x135c)]['kind'] == 'effect'
    assert by[(850,0x9d)]['opcode'] == 0x69 and by[(145,0x402)]['opcode'] == 0x4b
    assert not any(r['code'] == 'UNKNOWN_SCRIPT_COMMAND' for r in s['refusals'])
    assert s['structural_complete']
    assert not s["complete"]
    assert {tuple(e["to"]) for e in s["edges"] if e["from"] == [850,0x32]} == {(850,0x39),(850,0x3f)}


def native_arm9(project):
    start,_,_,size = struct.unpack_from("<4I",project.blob,0x20)
    return ndspy.codeCompression.decompress(project.blob[start:start+size])


def test_movement_byte_pattern_after_end_is_not_a_route(project):
    raw = struct.pack("<IH",2,0xfd13)+b"\x02\x00"+bytes.fromhex("5e000000000000004b000100fe000000")
    result = script_audit(native_arm9(project),{850:raw},{"npcs":[],"triggers":[],"backgrounds":[]})
    assert len(result["decoded"]) == 1 and not result["movement_references"]


def test_branch_into_operand_refused(project):
    raw = struct.pack("<IH",2,0xfd13)+struct.pack("<HBi",0x1c,0,-4)+b"\x02\x00\x00"
    with pytest.raises(EditorError) as error:
        script_audit(native_arm9(project),{850:raw},{"npcs":[],"triggers":[],"backgrounds":[]})
    assert error.value.code == "SCRIPT_CONTROL_FLOW_CONFLICT"


def test_entry_table_relative_base_and_aliases():
    raw = struct.pack("<IIH",6,2,0xfd13)+b"\x02\x00"
    assert entry_table(raw) == ([10,10],10)


@pytest.mark.parametrize("raw",[struct.pack("<IH",900,0xfd13),struct.pack("<IH",0,0xfd13),b"\x00"])
def test_bad_script_entry_refused(raw):
    with pytest.raises(EditorError):
        entry_table(raw)


def test_combined_three_object_proposal_is_static_only(definition):
    report = assess_layout(definition,PROPOSAL)
    assert not report["static_collision_conflicts"]
    assert report["static_connections"]["preserved"] and report["current_static_connections"]["preserved"]
    assert report["retained_ambiguous_cells"] == [[561,399],[561,400],[561,401]]
    assert len(report["hypothetical_changed_flag_cells"]) == 15
    assert not report["qualified"]
    assert {r["code"] for r in report["refusals"]} == {"OWNERSHIP_AMBIGUOUS","SCRIPT_PROOF_INCOMPLETE"}


def test_static_event_conflict_still_refuses(definition):
    q = copy.deepcopy(definition)
    next(a for a in q["anchors"] if a["anchor"] == PROPOSAL["5:12"])["static_event_conflicts"] = [{"kind":"triggers","id":0}]
    assert any(r["code"] == "DESTINATION_EVENT_CONFLICT" for r in assess_layout(q,PROPOSAL)["refusals"])


@pytest.mark.parametrize("anchor",[[551.5,399],[True,399.5],[float('nan'),399.5],None])
def test_invalid_anchor_refuses(definition,anchor):
    layout = {**PROPOSAL,"5:12":anchor}
    with pytest.raises(EditorError):
        assess_layout(definition,layout)


def test_no_single_planter_substitution(definition):
    with pytest.raises(EditorError) as error:
        assess_layout(definition,{"5:14":[560.5,404.5]})
    assert error.value.code == "INCOMPLETE_LAYOUT"


def test_dependency_digest_and_exact_before_values(project,definition):
    assert canonical_hash(definition["dependencies"]) == definition["dependencies_sha256"]
    for ref in definition["dependencies"]["resources"].values():
        assert digest(resource(project.blob,ref["archive"],ref["member"])[1]) == ref["sha256"]
    for s in definition["sources"].values():
        assert digest(bytes.fromhex(s["record_before"])) == s["record_sha256"]
    for r in definition["terrain"]["records"]:
        assert digest(bytes.fromhex(r["before"])) == r["sha256"]


@pytest.mark.parametrize("tamper",["permission","owner","frontiers","hash"])
def test_regeneration_refuses_tampered_json(project,definition,tamper):
    q = copy.deepcopy(definition)
    if tamper == "permission": q["sources"]["5:12"]["cells"][0]["before"] = "0000"
    elif tamper == "owner": q["sources"]["5:13"]["ownership_qualified"] = True
    elif tamper == "frontiers": q["scripts"]["refusals"] = []
    else: q["dependencies_sha256"] = "0"*64
    with pytest.raises(EditorError) as error:
        require_layout(project,q,PROPOSAL,5)
    assert error.value.code == "QUALIFICATION_CHANGED"


def test_stale_revision_refuses_before_qualification(project,definition):
    with pytest.raises(EditorError) as error:
        require_layout(project,definition,PROPOSAL,4)
    assert error.value.code == "STALE_REVISION"


def test_complete_boundary_always_refuses_and_preserves_project(project,definition):
    original = project.path.read_bytes()
    with pytest.raises(EditorError) as error:
        require_layout(project,definition,PROPOSAL,5)
    assert error.value.code == "UNQUALIFIED_LAYOUT"
    assert project.path.read_bytes() == original


def test_legacy_m3_m4_records_still_validate_without_conversion(project):
    original = project.path.read_bytes()
    project._validate_state(project.doc)
    legacy = json.loads((ROOT/'evidence/m4/before/projects/cherrygrove/project.json').read_text())
    assert legacy['revision'] == 4
    project._validate_state(legacy)
    assert project.doc['revision'] == 5 and len(project.doc['history']) == 3
    assert project.path.read_bytes() == original


def test_house_38_control_has_independent_door_mismatch(definition):
    proof = definition['sources']['5:13']['ownership_investigation']
    assert len(proof['body_door_pairs']) == 12
    assert len(proof['body_door_mismatches']) == 1
    a,b = proof['body_door_mismatches'][0]
    assert (a['offset'],a['before'],b['offset'],b['before']) == (1152,'6980',222,'0080')
    assert proof['source_east_cell_x_relative_to_house'] != proof['control_east_cell_x_relative_to_house']
    assert len(proof['compatible_ownership_hypotheses']) == 2
    assert all(not c['clear_allowed'] for c in definition['sources']['5:13']['cells'])


def test_map_initializer_and_zero_script_are_rooted(definition):
    s = definition['scripts']; init = s['initialization']
    assert init['records'] == [{'offset':0,'type':2,'script_id':11,'before':'020b000000',
                              'reserved':'0000','dispatched_entry':{'member':850,'entry_index':10,'offset':0x2e}}]
    assert init['trailing_before'] == '000000'
    assert next(r for r in s['event_dispatch'] if r['kind']=='npcs' and r['id']==4) == {
        'kind':'npcs','id':4,'script_id':0,'member':140,'entry_index':0,'offset':6}
    assert not s['complete']  # selection is not proof of runtime applicability


@pytest.mark.parametrize('raw',[b'',b'\x02\x0b',bytes.fromhex('020b000000')])
def test_initializer_truncation_refuses(raw):
    with pytest.raises(EditorError): initialization(raw)


def test_conditional_initializer_requires_its_table():
    with pytest.raises(EditorError) as error:
        initialization(bytes.fromhex('010000000000'))
    assert error.value.code == 'INIT_CONDITIONAL_UNRESOLVED'


def test_initializer_preserves_reserved_and_trailing_bytes():
    result = initialization(bytes.fromhex('020b00cafe00beef'))
    assert result['records'][0]['reserved'] == 'cafe'
    assert result['trailing_before'] == '00beef'


@pytest.mark.parametrize('operand,kind,known',[(255,'object_id',True),(253,'object_id',True),
                         (0xf1,'context_object',False),(0xf2,'movement_type_lookup',False),
                         (0x4000,'variable',False)])
def test_actor_resolution_distinguishes_lookup_operands(operand,kind,known):
    r = actor_reference(operand)
    assert r['kind'] == kind and r['resolved'] == known


def test_script_and_movement_4b_have_different_dispatchers(definition):
    s = definition['scripts']
    script = next(r for r in s['native_evidence']['script_commands'] if r['opcode']==0x4b)
    action = next(r for r in s['movement_actions'] if r['opcode']==0x4b)
    assert script['handler'] == 0x02049261 and script['operand_bytes'] == 2
    assert action['handlers'] == [0x02062f95,0x02062f6d,0x02062471]
    stream = next(r for r in s['movement_streams'] if r['member']==850 and r['offset']==0x350)
    assert stream['tile_delta'] == [0,0] and not stream['dynamic_qualified']


def test_movement_repeats_cover_every_intermediate_tile(project):
    actions = movement_definitions(native_arm9(project))
    # south 2, east 3, north 1, west 2; delay, facing lock, visual effect.
    raw = b''.join(struct.pack('<2H',op,n) for op,n in [(13,2),(19,3),(12,1),(18,2),
                                                         (63,2),(71,1),(75,1),(254,0)])
    s = movement_stream(raw,0,actions)
    assert s['relative_swept_tiles'] == [[0,0],[0,1],[0,2],[1,2],[2,2],[3,2],[3,1],[2,1],[1,1]]
    assert s['tile_delta'] == [1,1] and s['before'] == raw.hex()
    assert actions[13]['interpolation_frames'] == 8 and actions[19]['interpolation_frames'] == 4
    assert s['stream_decoded'] and not s['dynamic_qualified']


@pytest.mark.parametrize('records,code',[
    ([(254,0)],'MOVEMENT_STREAM_INVALID'), ([(13,0),(254,0)],'MOVEMENT_STREAM_INVALID'),
    ([(13,257),(254,0)],'MOVEMENT_STREAM_INVALID'), ([(13,1),(254,1)],'MOVEMENT_STREAM_INVALID'),
    ([(0x51,1),(254,0)],'UNKNOWN_MOVEMENT_ACTION')])
def test_unsupported_movement_refuses(project,records,code):
    with pytest.raises(EditorError) as error:
        movement_stream(b''.join(struct.pack('<2H',*r) for r in records),0,movement_definitions(native_arm9(project)))
    assert error.value.code == code


def test_movement_truncation_and_limit_refuse(project):
    actions = movement_definitions(native_arm9(project))
    with pytest.raises(EditorError): movement_stream(bytes.fromhex('0d000100fe00'),0,actions)
    with pytest.raises(EditorError) as error:
        movement_stream(bytes.fromhex('0d000100fe000000'),0,actions,limit=1)
    assert error.value.code == 'MOVEMENT_STREAM_INVALID'


def test_changed_native_movement_dispatch_refuses(project):
    b = bytearray(native_arm9(project))
    struct.pack_into('<I',b,0xfd2d8+13*4,struct.unpack_from('<I',b,0xfd2d8+14*4)[0])
    with pytest.raises(EditorError,match='movement dispatch differs'): movement_definitions(b)


def script_member(body):
    return struct.pack('<IH',2,0xfd13)+body


def test_local_call_return_and_jump_do_not_scan_dead_bytes(project):
    # 6: call 22; 12: jump 24; 18..21: unreachable unknown data; 22: return; 24: End.
    raw = script_member(struct.pack('<HiHi',0x1a,10,0x16,6)+b'\xff'*4+struct.pack('<HH',0x1b,2))
    s = script_audit(native_arm9(project),{850:raw},{'npcs':[],'triggers':[],'backgrounds':[]})
    assert {r['offset'] for r in s['decoded']} == {6,12,22,24}
    assert s['potential_call_returns'] == [[850,12]]
    assert s['structural_complete'] and not s['complete']


def test_script_target_inside_movement_data_refuses(project):
    # Two entry roots: movement command at 10, unknown SCRIPT 0x0d at movement offset 20.
    raw = struct.pack('<IIH',6,12,0xfd13)+struct.pack('<HHiH',0x5e,255,2,2)+bytes.fromhex('0d000100fe000000')
    with pytest.raises(EditorError) as error:
        script_audit(native_arm9(project),{850:raw},{'npcs':[],'triggers':[],'backgrounds':[]})
    assert error.value.code == 'SCRIPT_CONTROL_FLOW_CONFLICT'


def test_projection_unknown_command_is_a_refusal(project):
    event = {'id':0,'script':1,'x':100,'z':200,'width':1,'height':1}
    s = script_audit(native_arm9(project),{850:script_member(b'\xff\xff')},
                     {'npcs':[],'triggers':[event],'backgrounds':[]})
    assert not s['structural_complete']
    assert s['event_projection']['refusals'][0]['code'] == 'PROJECTED_SCRIPT_UNRESOLVED'


def test_wait_applies_asynchronous_actor_group_before_next_launch(project):
    body = (struct.pack('<HHiHHiHHHiHH',0x5e,255,22,0x5e,0,22,0x5f,0x5e,255,4,0x5f,2)
            +bytes.fromhex('0d000200fe0000000e000100fe000000'))
    view = {'npcs':[{'id':0,'script':1,'x':3,'z':4,'range_x':0,'range_z':0}],
            'triggers':[{'id':0,'script':1,'x':100,'z':200,'width':1,'height':1}],'backgrounds':[]}
    s = script_audit(native_arm9(project),{850:script_member(body)},view)
    launches = [r for r in s['event_projection']['launches'] if r['scenario']=='trigger:0@100,200']
    assert [(r['actor'],r['start'],r['end'],r['pending_actors']) for r in launches] == [
        (255,[100,200],[100,202],[255]),(0,[3,4],[2,4],[0,255]),(255,[100,202],[100,204],[255])]
    assert not s['event_projection']['refusals'] and not s['complete']


def test_both_authored_event_states_keep_follower_uncertainty(definition):
    s = definition['scripts']
    for field in ('event_projection','current_event_projection'):
        p = s[field]
        assert p['scenarios'] == 68 and len(p['launches']) == 115
        assert len(p['refusals']) == 4
        assert {(r['member'],r['offset'],r['actor']) for r in p['refusals']} == {(850,0x9fd,253)}
        assert p['assumptions'] and not p['dynamic_qualified']
    baseline = {r['scenario'] for r in s['event_projection']['endpoints'] if r['scenario'].startswith('npcs:1@')}
    current = {r['scenario'] for r in s['current_event_projection']['endpoints'] if r['scenario'].startswith('npcs:1@')}
    assert baseline and current and baseline.isdisjoint(current)


def test_original_and_intermediate_layouts_hit_distinct_projected_routes(definition):
    original = assess_layout(definition,ORIGINAL_PROPOSAL)
    assert {tuple(c) for r in original['projected_script_conflicts'] for c in r['cells']} == {(556,404)}
    assert {r['actor'] for r in original['projected_script_conflicts']} == {0,255}
    assert {r['event_state'] for r in original['projected_script_conflicts']} == {'baseline','revision5'}
    middle = assess_layout(definition,{**PROPOSAL,'5:14':[561.5,404.5]})
    assert {tuple(c) for r in middle['projected_script_conflicts'] for c in r['cells']} == {(561,403)}
    assert {r['command_offset'] for r in middle['projected_script_conflicts']} == {0x9ba}
    final = assess_layout(definition,PROPOSAL)
    assert not final['projected_script_conflicts'] and not final['qualified']


def test_pinned_native_overlay_and_all_researched_bytes(project,definition):
    s = definition['scripts']; evidence = s['native_evidence']
    overlay,ref = overlay_image(project.blob,1)
    assert ref == evidence['overlay']
    images = {'arm9':native_arm9(project),'overlay:1':overlay}
    slices = evidence['researched_slices']+[r[k] for r in evidence['script_commands'] for k in ('dispatch','handler_bytes')]
    for r in slices:
        before = images[r['image']][r['offset']:r['offset']+r['size']]
        assert before.hex() == r['before'] and digest(before) == r['sha256']
    assert len(evidence['script_commands']) == 83
    assert canonical_hash(evidence) == definition['dependencies']['native_evidence_sha256']


def test_erased_frontiers_and_forged_completeness_cannot_grant(definition):
    q = copy.deepcopy(definition)
    q['scripts']['complete'] = True; q['scripts']['refusals'] = []; q['write_qualified'] = True
    for source in q['sources'].values(): source['refusals'] = []; source['ownership_qualified'] = True
    r = assess_layout(q,PROPOSAL)
    assert not r['qualified'] and any(f['code']=='SCRIPT_PROOF_INCOMPLETE' for f in r['refusals'])


def test_tampered_native_effect_provenance_refuses(project,definition):
    q = copy.deepcopy(definition)
    q['scripts']['native_evidence']['researched_slices'][-1]['before'] = '00'
    with pytest.raises(EditorError) as error: require_layout(project,q,PROPOSAL,5)
    assert error.value.code == 'QUALIFICATION_CHANGED'


def test_held_wait_can_succeed_during_normal_movement_and_pause_does_not_stop_held():
    flags = ACTIVE | NORMAL_BUSY | PAUSED
    assert held_complete(flags) and not stream_ready(flags)
    assert actor_update_lane(flags) == 'paused'
    assert actor_update_lane(flags | HELD, normal_gate=False) == 'held'
    assert not held_complete(flags | HELD)
    assert held_complete(flags | HELD | FINISHED)
    assert not stream_ready(flags | HELD | FINISHED)  # normal busy is independently tested
    assert stream_ready(ACTIVE | HELD | FINISHED)
    assert not stream_ready(0)
    assert actor_update_lane(ACTIVE | HELD, manager_disabled=True) == 'disabled'


@pytest.mark.parametrize('phase,next_phase',[(0,1),(1,1),(2,2),(3,2)])
def test_follower_mailbox_overwrites_instead_of_accumulating_moves(phase,next_phase):
    first = enqueue_follower_action(phase,0x58,(10,20))
    assert first == {'phase':next_phase,'action':0x58,'target':[10,20]}
    second = enqueue_follower_action(first['phase'],13,(11,20))
    assert second['action'] == 13 and second['target'] == [11,20]
    assert first['target'] == [10,20]


def test_follower_types_have_distinct_direction_rules_and_no_catchup_loop():
    assert follower_step(0x37,(8,8),(4,4),12) == (7,8)  # X before Z, one step
    assert follower_step(0x37,(4,8),(4,4),13) == (4,7)
    assert follower_step(0x37,(4,4),(4,4),19) == (4,4)
    assert follower_step(0x38,(8,8),(4,4),12) == (8,7)  # copies north, not chase west
    assert follower_step(0x38,(8,8),(4,4),0x5b) == (9,8)
    with pytest.raises(EditorError) as error:
        follower_step(0x30,(8,8),(4,4),12)
    assert error.value.code == 'FOLLOWER_QUEUE_STATE_REQUIRED'


def test_conditional_follower_envelope_includes_completion_phase_and_missed_updates():
    stream = {'records':[{'kind':'step','opcode':15,'repeats':2,'tile_delta':[1,0]}]}
    exact = conditional_follower_sweep(stream,(0,0),[(0,0)],allow_missed_updates=False)
    assert exact['end_tiles'] == [[1,0]]
    broad = conditional_follower_sweep(stream,(0,0),((x,0) for x in (0,)),allow_missed_updates=True)
    assert broad['follower_starts'] == [[0,0]]
    assert broad['end_tiles'] == [[0,0],[1,0],[2,0]]
    assert not broad['dynamic_qualified'] and len(broad['conditions']) == 4
    copied = conditional_follower_sweep(stream,(0,0),[(0,0),(0,1)],0x38,False)
    assert copied['end_tiles'] == [[2,0],[2,1]]
    assert copied['swept_tiles'] == [[0,0],[0,1],[1,0],[1,1],[2,0],[2,1]]


@pytest.mark.parametrize('starts,op,kind,code',[
    (None,15,'step','FOLLOWER_ENTRY_STATE_REQUIRED'),
    ([(0,0)],75,'effect','FOLLOWER_EFFECT_UNRESOLVED'),
    ([(0,0)],32,'step','FOLLOWER_ACTION_UNRESOLVED'),
])
def test_conditional_follower_model_refuses_unproved_entry_or_actions(starts,op,kind,code):
    with pytest.raises(EditorError) as error:
        conditional_follower_sweep({'records':[{'kind':kind,'opcode':op,'repeats':1,'tile_delta':[1,0]}]},
                                   (0,0),starts)
    assert error.value.code == code


def test_all_real_child_release_tails_are_atomic_but_generic_item_paths_need_domains(definition):
    result = child_serialization(definition['scripts'])
    assert len(result['roots']) == 7 and not result['complete']
    assert all(r['release_tails_atomic'] for r in result['roots'])
    assert sum(r['serializable'] for r in result['roots']) == 5
    assert {(r['member'],r['offset']) for r in result['roots'] if not r['serializable']} == {(3,0x4f0),(3,0x85d)}
    assert sum(len(r['release_end_pairs']) for r in result['roots']) == 9
    assert {v['offset'] for r in result['roots'] for v in r['early_end_sites']} == {0x56e,0x967}


def child_audit(tail_kind='end',tail_before='0200'):
    return {'event_dispatch':[{'kind':'triggers','id':0,'script_id':1,'member':850,'offset':6}],
            'decoded':[
        {'member':850,'offset':6,'opcode':0x14,'kind':'dispatch','before':'1400d007',
         'dispatched_entry':{'member':3,'offset':6}},
        {'member':850,'offset':10,'opcode':2,'kind':'end','before':'0200'},
        {'member':3,'offset':6,'opcode':0x15,'kind':'child_release','before':'1500'},
        {'member':3,'offset':8,'opcode':2,'kind':tail_kind,'before':tail_before}]}


def test_child_release_with_wait_or_missing_end_cannot_be_serialized():
    good = child_serialization(child_audit())
    assert good['complete'] and good['roots'][0]['release_end_pairs'] == [[6,8]]
    bad = child_serialization(child_audit('wait_movement','5f00'))
    assert not bad['complete'] and 'CHILD_RELEASE_TAIL_NOT_ATOMIC' in bad['roots'][0]['refusals']


def test_end_inside_child_local_call_does_not_resume_parent(project):
    audit = child_audit()
    # Child calls End at 16. The sequential release at 12 must not be treated as reached.
    audit['decoded'][2:] = [
        {'member':3,'offset':6,'opcode':26,'kind':'call','before':struct.pack('<Hi',26,4).hex()},
        {'member':3,'offset':12,'opcode':21,'kind':'child_release','before':'1500'},
        {'member':3,'offset':14,'opcode':2,'kind':'end','before':'0200'},
        {'member':3,'offset':16,'opcode':2,'kind':'end','before':'0200'}]
    result = child_serialization(audit)
    assert result['roots'][0]['early_end_sites'] == [{'offset':16,'return_stack':[12]}]
    view = {'npcs':[],'backgrounds':[],'triggers':[{'id':0,'x':1,'z':1,'width':1,'height':1}]}
    projection = project_event_routes(audit,[],view,native_arm9(project))
    assert not projection['endpoints']
    assert projection['refusals'][0]['code'] == 'PROJECTED_CHILD_ORDER_UNPROVED'


def test_child_comparison_does_not_overwrite_parent_and_child_does_not_inherit_it(project):
    audit = child_audit()
    audit['decoded'] = [
        {'member':850,'offset':6,'opcode':17,'kind':'effect','before':'110001000100'},
        {'member':850,'offset':12,'opcode':20,'kind':'dispatch','before':'1400d007',
         'dispatched_entry':{'member':3,'offset':6}},
        {'member':850,'offset':16,'opcode':28,'kind':'branch','before':struct.pack('<HBi',28,1,4).hex()},
        {'member':850,'offset':23,'opcode':65535,'before':'ffff'},
        {'member':850,'offset':27,'opcode':2,'kind':'end','before':'0200'},
        {'member':3,'offset':6,'opcode':17,'kind':'effect','before':'110001000200'},
        {'member':3,'offset':12,'opcode':21,'kind':'child_release','before':'1500'},
        {'member':3,'offset':14,'opcode':2,'kind':'end','before':'0200'}]
    view = {'npcs':[],'backgrounds':[],'triggers':[{'id':0,'x':1,'z':1,'width':1,'height':1}]}
    p = project_event_routes(audit,[],view,native_arm9(project))
    assert len(p['endpoints']) == 1 and not p['refusals']
    audit['decoded'][5:] = [
        {'member':3,'offset':6,'opcode':28,'kind':'branch','before':struct.pack('<HBi',28,1,4).hex()},
        {'member':3,'offset':13,'opcode':21,'kind':'child_release','before':'1500'},
        {'member':3,'offset':15,'opcode':2,'kind':'end','before':'0200'},
        {'member':3,'offset':17,'opcode':21,'kind':'child_release','before':'1500'},
        {'member':3,'offset':19,'opcode':2,'kind':'end','before':'0200'}]
    p = project_event_routes(audit,[],view,native_arm9(project))
    assert not p['refusals']
    assert {r['offset'] for r in p['contracts_used'] if r['member']==3} == {13,17}


def test_native_item_inputs_exclude_early_end_only_when_supplied(project,definition):
    s = definition['scripts']; inputs = s['item_runtime']
    assert inputs['event_variables']['8001'] == {'32768':92,'32769':1,'32770':801}
    assert inputs['event_variables']['8225']['32768'] == 92
    assert {int(k):v['pocket'] for k,v in inputs['items'].items()} == {92:0,243:0}
    from sovereign_editor.formats import events
    view = events(resource(project.blob,'a/0/3/2',64)[1])
    erased = {**s,'item_runtime':{}}
    projection = project_event_routes(erased,s['movement_streams'],view,native_arm9(project))
    assert sum(r['code']=='PROJECTED_CHILD_END_WITHOUT_RELEASE' for r in projection['refusals']) == 20
    for field in ('event_projection','current_event_projection'):
        p = s[field]
        assert not any(r['code']=='PROJECTED_CHILD_END_WITHOUT_RELEASE' for r in p['refusals'])
        assert p['states_visited'] == 2428 and len(p['assumptions']) == 184
        assert len(p['launches']) == 115 and len(p['contracts_used']) == 76
        assert not p['dynamic_qualified']
    assert not any(r['code']=='PROJECTED_CHILD_END_WITHOUT_RELEASE' for r in s['refusals'])


def test_additional_runtime_and_item_bytes_are_pinned(project,definition):
    s = definition['scripts']; evidence = s['runtime_evidence']
    images = {'arm9':native_arm9(project)}
    for ref in evidence['overlays']:
        data, actual = overlay_image(project.blob,ref['overlay'])
        assert actual == ref
        images[f"overlay:{ref['overlay']}"] = data
    for ref in evidence['slices']:
        raw = images[ref['image']][ref['offset']:ref['offset']+ref['size']]
        assert raw.hex() == ref['before'] and digest(raw) == ref['sha256']
    for ref in s['item_runtime']['items'].values():
        raw = resource(project.blob,ref['archive'],ref['member'])[1]
        assert raw.hex() == ref['before'] and digest(raw) == ref['sha256']
    assert canonical_hash(evidence) == definition['dependencies']['runtime_evidence_sha256']
    assert canonical_hash(s['item_runtime']) == definition['dependencies']['item_runtime_sha256']


@pytest.mark.parametrize('target',['runtime_evidence','item_runtime'])
def test_tampered_runtime_contract_dependencies_refuse(project,definition,target):
    q = copy.deepcopy(definition)
    if target == 'runtime_evidence': q['scripts'][target]['slices'][0]['before'] = '00'
    else: q['scripts'][target]['items']['92']['pocket'] = 7
    with pytest.raises(EditorError) as error: require_layout(project,q,PROPOSAL,5)
    assert error.value.code == 'QUALIFICATION_CHANGED'
