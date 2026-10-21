// StartLux adapter for GPL-3.0 AutoCommission. Upstream modules are unmodified.
for (const module of ["constants.js", "lib/utils.js", "lib/ui.js", "lib/core.js",
  "lib/execute.js", "lib/dialog-processor.js", "lib/commission-basic.js",
  "lib/commission-recognition.js", "lib/commission-data.js", "lib/step-processor-loader.js"])
  eval(file.readTextSync(module));

// Require an unambiguous template match; archive the exact journal frame before
// navigation changes it. A failed recognition is never treated as incomplete.
CommissionBasic.detectCommissionStatusByImage = async function (index) {
  const button = Constants.COMMISSION_DETAIL_BUTTONS[index];
  let screen, crop, completed, pending, ca, cb;
  try {
    screen = captureGameRegion();
    crop = screen.deriveCrop(button.checkX, button.y - 30, button.checkWidth, 60);
    completed = file.ReadImageMatSync(Constants.COMPLETED_IMAGE_PATH);
    pending = file.ReadImageMatSync(Constants.UNCOMPLETED_IMAGE_PATH);
    const a = RecognitionObject.TemplateMatch(completed, 0, 0, button.checkWidth, 60);
    const b = RecognitionObject.TemplateMatch(pending, 0, 0, button.checkWidth, 60);
    // Current handbook pending pins score ~0.805 against the upstream image.
    // Keep completion strict; a pending classification never proves success.
    a.threshold = 0.9; b.threshold = 0.75;
    ca = crop.find(a); cb = crop.find(b);
    const done = !ca.isEmpty(), todo = !cb.isEmpty();
    const status = done === todo ? "unknown" : done ? "completed" : "uncompleted";
    if (status === "unknown") {
      // Diagnostic files live only in the host's disposable per-scan directory.
      // They never substitute for an accepted journal completion observation.
      file.WriteImageSync("Data/startlux-status-" + (index + 1) + ".png", crop.SrcMat);
      file.WriteImageSync("Data/startlux-journal-" + (index + 1) + ".png", screen.SrcMat);
    }
    // The host encodes this very Mat, rather than taking a later screenshot.
    agentJournal.Record(index + 1, status, screen.SrcMat);
    return status;
  } finally {
    ca?.dispose(); cb?.dispose();
    crop?.dispose(); screen?.dispose(); completed?.dispose(); pending?.dispose();
  }
};

(async function () {
  setGameMetrics(1920, 1080, 1);
  try {
    if (settings.operation === "scan") {
      const rows = await Core.Identification();
      agentJournal.SetRows(JSON.stringify(rows));
    } else if (settings.operation === "execute") {
      const quest = JSON.parse(settings.questJson);
      StepProcessorLoader.loadStepProcessors();
      await genshin.returnMainUi();
      // Recovery is free at the configured statue; do not enter another fight
      // with the critical HP observed during the first live commissioning run.
      if (quest.type === "fight") await genshin.tpToStatueOfTheSeven();
      const ok = quest.type === "fight"
        ? await Execute.executeFightCommission(quest)
        : await Execute.executeTalkCommission(quest.name, quest.location);
      if (!ok) throw new Error("commission_executor_failed");
      // Execution return values never mark a commission complete.
    } else if (settings.operation === "reward-check") {
      await genshin.returnMainUi();
      if (!await UI.enterCommissionScreen()) throw new Error("journal_open_failed");
      await sleep(700);
      agentJournal.RecordReward();
    } else throw new Error("unsupported_agent_operation");
  } finally {
    await genshin.returnMainUi();
    await sleep(400);
  }
})();
