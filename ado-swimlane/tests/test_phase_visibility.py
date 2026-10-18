"""Execute the real phase controller with multi-lane DOM and storage failures."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

BOARD = Path(__file__).resolve().parent.parent / "board" / "board.html"


@pytest.mark.parametrize("saved,blocked,expected", [
    ("[]", False, []),
    ("[0,3,6]", False, [0, 3, 6]),
    ("[4,4]", False, [4]),
    ("[0,1,2,3,4,5,6]", False, list(range(7))),
    ('[null,-1,7,1.5,"2",true,4]', False, [4]),
    ('{"phase":2}', False, []),
    ("broken json", False, []),
    ("[1]", True, []),
])
def test_phase_visibility_persistence_and_alignment(saved, blocked, expected):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the phase UI regression")
    html = BOARD.read_text(encoding="utf-8-sig")
    script = html.split("// Phase visibility.", 1)[1].split("// End phase visibility.", 1)[0]
    script = script[script.index("(function ()"): ]
    harness = r"""
const fixture = FIXTURE;
function element(kind) {
  const classes = new Set([kind]);
  return {style: {}, attrs: {}, children: [], hidden: false,
    classList: {contains: c => classes.has(c), toggle: (c,on) => on ? classes.add(c) : classes.delete(c)},
    setAttribute(k,v) {this.attrs[k]=v;},
    addEventListener(k,v) {this[k]=v;},
    appendChild(b) {this.children.push(b);},
    querySelector() {return this.children[0];}};
}
const corner=element('phasehead'), headers=Array.from({length:7},()=>element('phasehead'));
const labels=Array.from({length:3},()=>element('lanelabel'));
const rows=labels.map(label=>{
  const cells=Array.from({length:7},()=>element('cell'));
  label.nextElementSibling=cells[0];
  cells.forEach((cell,i)=>cell.nextElementSibling=cells[i+1]);
  return cells;
});
const board={style:{}, querySelectorAll: s=>s==='.phasehead' ? [corner,...headers] : labels,
  querySelector: ()=>corner};
const document={createElement: ()=>element('button')};
let value=fixture.saved, draws=0;
const localStorage={getItem: ()=>{if(fixture.blocked) throw Error('blocked'); return value;},
  setItem: (k,v)=>{if(fixture.blocked) throw Error('blocked'); value=v;}};
function draw(){draws++;}
SCRIPT
function snapshot(){
  return {hidden: rows[0].map((c,i)=>c.hidden ? i : -1).filter(i=>i!==-1),
    rows:rows.map(row=>row.map(c=>[c.hidden,c.style.gridArea])),
    labels:labels.map(l=>l.style.gridArea),
    buttons:headers.map(h=>({type:h.children[0].type, text:h.children[0].textContent, attrs:h.children[0].attrs})),
    columns:board.style.gridTemplateColumns, minimum:board.style.minWidth};
}
const initial=snapshot();
headers.forEach((header,phase)=>{
  const before=rows[0][phase].hidden;
  header.children[0].click();
  if(rows.some(row=>row[phase].hidden===before)) throw Error('toggle failed');
});
const toggled=snapshot();
headers.forEach(header=>header.children[0].click());
console.log(JSON.stringify({initial,toggled,restored:snapshot(),value,draws}));
""".replace("FIXTURE", json.dumps({"saved": saved, "blocked": blocked})).replace("SCRIPT", script)
    result = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    actual = json.loads(result.stdout)
    initial = actual["initial"]
    assert initial["hidden"] == expected
    assert actual["restored"] == initial
    assert actual["toggled"]["hidden"] == [p for p in range(7) if p not in expected]
    assert initial["labels"] == ["2 / 1", "3 / 1", "4 / 1"]
    for lane, row in enumerate(initial["rows"]):
        assert [cell[1] for cell in row] == [f"{lane + 2} / {p + 2}" for p in range(7)]
        for phase, (hidden, _) in enumerate(row):
            assert hidden == (phase in expected)
    for phase, button in enumerate(initial["buttons"]):
        hidden = phase in expected
        assert button["type"] == "button"
        assert button["attrs"]["aria-expanded"] == str(not hidden).lower()
        assert button["attrs"]["aria-label"] == f"{'Show' if hidden else 'Hide'} phase P{phase}"
        assert button["attrs"]["aria-controls"].split() == [f"phase-{phase}-lane-{lane}" for lane in range(3)]
    assert actual["draws"] == 15
    if not blocked:
        assert sorted(json.loads(actual["value"])) == sorted(expected)


def test_arrows_skip_hidden_endpoints():
    html = BOARD.read_text(encoding="utf-8-sig")
    assert "!s.getClientRects().length || !t.getClientRects().length" in html
    assert 'new ResizeObserver(draw).observe(board)' in html


def test_phase_bar_chips_bulk_actions_and_collapse_modes():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the phase UI regression")
    html = BOARD.read_text(encoding="utf-8-sig")
    assert '<div class="phasebar" id="phase-bar"' in html
    script = html.split("// Phase visibility.", 1)[1].split("// End phase visibility.", 1)[0]
    script = script[script.index("(function ()"):]
    harness = r"""
function element(kind) {
  const classes = new Set([kind]);
  return {style: {}, attrs: {}, children: [], hidden: false, textContent: '',
    classList: {contains: c => classes.has(c), toggle: (c,on) => on ? classes.add(c) : classes.delete(c)},
    setAttribute(k,v) {this.attrs[k]=v;},
    addEventListener(k,v) {this[k]=v;},
    appendChild(b) {this.children.push(b);},
    querySelector(sel) {
      if (sel === '.pname') return {textContent: 'PhaseName'};
      return this.children[0];
    }};
}
const corner=element('phasehead'), headers=Array.from({length:7},()=>element('phasehead'));
const labels=Array.from({length:2},()=>element('lanelabel'));
const rows=labels.map(label=>{
  const cells=Array.from({length:7},()=>element('cell'));
  label.nextElementSibling=cells[0];
  cells.forEach((cell,i)=>cell.nextElementSibling=cells[i+1]);
  return cells;
});
const bar=element('div');
const board={style:{}, classList: element('board').classList,
  querySelectorAll: s=>s==='.phasehead' ? [corner,...headers] : labels,
  querySelector: ()=>corner};
const document={
  createElement: t=>element(t),
  createTextNode: t=>({textContent: t}),
  getElementById: id=>id==='phase-bar' ? bar : null
};
const store={};
const localStorage={getItem: k=>store[k]||null, setItem: (k,v)=>{store[k]=v;}};
let draws=0;
function draw(){draws++;}
SCRIPT
const left=bar.children[0], right=bar.children[1];
const chips=left.children.slice(1);
const [collapseP0P2, collapseAll, expandAll, modeBtn]=right.children;
collapseP0P2.click();
const afterP0P2={hidden: JSON.parse(store['swimlane.hidden-phases.v1']), cols: board.style.gridTemplateColumns, min: board.style.minWidth};
modeBtn.click();
const afterZero={mode: store['swimlane.collapse-mode.v1'], cols: board.style.gridTemplateColumns, min: board.style.minWidth, zeroCls: board.classList.contains('phase-mode-zero')};
headers[1].click({target: headers[1]});
const afterHeaderExpand=JSON.parse(store['swimlane.hidden-phases.v1']);
collapseAll.click();
const afterCollapseAll=JSON.parse(store['swimlane.hidden-phases.v1']);
expandAll.click();
const afterExpandAll=JSON.parse(store['swimlane.hidden-phases.v1']);
chips[4].click();
const afterChip4={hidden: JSON.parse(store['swimlane.hidden-phases.v1']), pressed4: chips[4].attrs['aria-pressed']};
console.log(JSON.stringify({chipCount: chips.length, afterP0P2, afterZero, afterHeaderExpand, afterCollapseAll, afterExpandAll, afterChip4}));
""".replace("SCRIPT", script)
    result = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out["chipCount"] == 7
    assert out["afterP0P2"]["hidden"] == [0, 1, 2]
    assert "38px 38px 38px" in out["afterP0P2"]["cols"]
    assert out["afterZero"]["mode"] == "zero"
    assert out["afterZero"]["zeroCls"] is True
    assert "0px 0px 0px" in out["afterZero"]["cols"]
    assert out["afterHeaderExpand"] == [0, 2]
    assert out["afterCollapseAll"] == [0, 1, 2, 3, 4, 5, 6]
    assert out["afterExpandAll"] == []
    assert out["afterChip4"] == {"hidden": [4], "pressed4": "false"}

