"""FC4 (input register) read support: dispatch, homogeneous batching, schema."""
from multibus.config import SelectedRegister, normalize_register_type
from multibus.modbus_client import RegisterPoller, ModbusConnection, ModbusConfig
from multibus.register_parser import RegisterParser
from multibus.csv_import import parse_csv
from multibus.device_template import parse_template


def _reg(addr, rtype='holding', dt='uint16'):
    return SelectedRegister(address=addr, name=f'r{addr}', label='', unit='',
                            data_type=dt, poll_group='normal', register_type=rtype)


def test_read_groups_never_mix_holding_and_input():
    # adjacent addresses, but different register types → must be separate groups
    regs = [_reg(0, 'holding'), _reg(1, 'holding'), _reg(2, 'input'), _reg(3, 'input')]
    poller = RegisterPoller('t', 1, regs, None, RegisterParser('big'), lambda *a: None)
    groups = poller._create_read_groups()
    assert len(groups) == 2
    for g in groups:
        assert len({getattr(r, 'register_type', 'holding') for r in g['registers']}) == 1
        assert g['register_type'] in ('holding', 'input')


def test_max_gap_controls_batch_merging():
    class _Conn:
        def __init__(self, g): self.config = ModbusConfig(max_gap=g)
    regs = [_reg(100), _reg(110)]                    # a 9-register unmapped gap
    # default (10): bridged into a single batch read (optimizes contiguous maps)
    p10 = RegisterPoller('t', 1, regs, _Conn(10), RegisterParser('big'), lambda *a: None)
    assert len(p10._create_read_groups()) == 1
    # strict (0): read separately, so a gapped slave never gets an over-span block
    p0 = RegisterPoller('t', 1, regs, _Conn(0), RegisterParser('big'), lambda *a: None)
    assert len(p0._create_read_groups()) == 2


class _FakeResult:
    def __init__(self, regs): self.registers = regs
    def isError(self): return False


class _FakeClient:
    def __init__(self): self.calls = []
    def is_socket_open(self): return True
    def connect(self): return True
    def close(self): pass
    def read_holding_registers(self, address, count, slave):
        self.calls.append(('hr', address, count)); return _FakeResult([0] * count)
    def read_input_registers(self, address, count, slave):
        self.calls.append(('ir', address, count)); return _FakeResult([1] * count)


def test_read_registers_dispatches_fc3_and_fc4():
    conn = ModbusConnection(ModbusConfig())
    conn.client = _FakeClient()
    conn.connected = True
    conn.read_registers(10, 2, 'holding')
    conn.read_registers(20, 2, 'input')
    assert conn.client.calls == [('hr', 10, 2), ('ir', 20, 2)]


def test_read_registers_defaults_to_holding():
    conn = ModbusConnection(ModbusConfig())
    conn.client = _FakeClient()
    conn.connected = True
    conn.read_registers(5, 1)                 # no register_type → FC3
    assert conn.client.calls == [('hr', 5, 1)]


def test_normalize_register_type():
    for v in ('input', 'ir', 'fc4', '4', 'Input', 'FC4', 'input_register'):
        assert normalize_register_type(v) == 'input'
    for v in ('holding', 'hr', 'fc3', '3', '', None, 'xyz'):
        assert normalize_register_type(v) == 'holding'


def test_csv_register_type_column():
    r = parse_csv("address,name,fc\n1,A,4\n2,B,holding\n3,C,input\n")
    m = {x['name']: x for x in r['registers']}
    assert m['A'].get('register_type') == 'input'
    assert 'register_type' not in m['B']       # holding is the default; not emitted
    assert m['C'].get('register_type') == 'input'


def test_template_register_type_roundtrip():
    tpl = {"device_template": {"id": "fc4_test", "name": "T", "registers": [
        {"address": 1, "name": "a", "register_type": "input"},
        {"address": 2, "name": "b"}]}}
    t = parse_template(tpl)
    assert t.registers[0].register_type == 'input'
    assert t.registers[1].register_type == 'holding'
    d = t.to_dict()['device_template']['registers']
    assert d[0].get('register_type') == 'input'
    assert 'register_type' not in d[1]          # holding omitted (byte-identical files)
