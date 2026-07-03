from ledger import kill_switch


def test_engage_disengage_cycle(tmp_path):
    switch = tmp_path / "KILL_SWITCH"
    assert not kill_switch.is_engaged(switch)
    kill_switch.engage(switch, reason="testing")
    assert kill_switch.is_engaged(switch)
    assert "testing" in switch.read_text()
    kill_switch.disengage(switch)
    assert not kill_switch.is_engaged(switch)


def test_bare_touched_file_engages(tmp_path):
    # Harry can `touch KILL_SWITCH` manually; an empty file must count.
    switch = tmp_path / "KILL_SWITCH"
    switch.write_text("")
    assert kill_switch.is_engaged(switch)


def test_disengage_when_absent_is_harmless(tmp_path):
    kill_switch.disengage(tmp_path / "KILL_SWITCH")
