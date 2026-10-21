// Offline ClearScript boundary checks; no capture, input or game is involved.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, 'agent-main.js'), 'utf8');

async function check(scores, expected) {
  const disposed = [], records = [], diagnostics = [];
  const frame = { id: 'captured-journal-frame' };
  const crop = { SrcMat: { id: 'crop' }, dispose() { disposed.push('crop'); },
    find(ro) { return { isEmpty: () => scores[ro.kind] < ro.threshold,
      dispose() { disposed.push('match-' + ro.kind); } }; } };
  const context = { CommissionBasic: {}, Constants: { COMMISSION_DETAIL_BUTTONS: [
    {checkX: 1450, y: 320, checkWidth: 150}], COMPLETED_IMAGE_PATH: 'completed',
    UNCOMPLETED_IMAGE_PATH: 'pending' },
    captureGameRegion: () => ({ SrcMat: frame, deriveCrop: () => crop,
      dispose() { disposed.push('screen'); } }),
    file: { readTextSync: () => '', ReadImageMatSync: kind => ({kind,
      dispose() { disposed.push('template-' + kind); }}),
      WriteImageSync: filename => diagnostics.push(filename) },
    RecognitionObject: { TemplateMatch: template => ({kind: template.kind}) },
    agentJournal: { Record(slot, status, image) { records.push({slot,status,image}); },
      SetRows() {} }, settings: {operation:'scan'},
    Core: { async Identification() {
      const status = await context.CommissionBasic.detectCommissionStatusByImage(0);
      assert.equal(status, expected);
      return [];
    } }, genshin: {returnMainUi:async()=>{}}, sleep:async()=>{}, setGameMetrics:()=>{} };
  await vm.runInNewContext(source, context);
  assert.equal(records.length, 1);
  assert.equal(records[0].status, expected);
  assert.strictEqual(records[0].image, frame);
  assert.equal(diagnostics.length, expected === 'unknown' ? 2 : 0);
  assert.equal(new Set(disposed).size, 6, 'release both matches, both templates and both regions');
}

async function checkExecution(type, healFails = false) {
  const calls = [];
  const context = {CommissionBasic: {}, file: {readTextSync:()=>''},
    settings: {operation:'execute',questJson:JSON.stringify({type,name:'fixture',location:'fixture'})},
    StepProcessorLoader:{loadStepProcessors(){}},
    genshin:{async returnMainUi(){calls.push('world');}, async tpToStatueOfTheSeven(){
      calls.push('heal'); if(healFails)throw new Error('healing failed');
    }}, Execute:{async executeFightCommission(){calls.push('fight');return true;},
      async executeTalkCommission(){calls.push('talk');return true;}},
    setGameMetrics(){}, sleep:async()=>{}};
  if(healFails) {
    await assert.rejects(vm.runInNewContext(source,context),/healing failed/);
    assert.deepEqual(calls,['world','heal','world']);
  } else {
    await vm.runInNewContext(source,context);
    assert.deepEqual(calls,type==='fight'?['world','heal','fight','world']:['world','talk','world']);
  }
}

(async () => {
  await check({completed:0.61,pending:0.805}, 'uncompleted');
  await check({completed:0.95,pending:0.4}, 'completed');
  await check({completed:0.95,pending:0.85}, 'unknown');
  await check({completed:0.4,pending:0.4}, 'unknown');
  await checkExecution('fight');
  await checkExecution('talk');
  await checkExecution('fight',true);
  process.stdout.write('7 adapter boundary checks passed\n');
})().catch(error => { console.error(error); process.exitCode = 1; });
