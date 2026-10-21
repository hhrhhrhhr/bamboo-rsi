using System.Windows;
using System.IO;
using Vosk;
using Newtonsoft.Json.Linq;
namespace StartLux.Desktop;
public partial class App : Application
{
    protected override void OnStartup(StartupEventArgs e)
    {
        if (e.Args.Length == 2 && e.Args[0] == "--self-test-voice")
        {
            try
            {
                Vosk.Vosk.SetLogLevel(-1);
                using var model = new Model(Path.Combine(AppContext.BaseDirectory, "voice-model"));
                using var recognizer = new VoskRecognizer(model, 16000);
                var pcm = new byte[32000]; // Real native decoder, mono PCM16 silence.
                recognizer.AcceptWaveform(pcm, pcm.Length);
                JObject.Parse(recognizer.FinalResult());
                File.WriteAllText(e.Args[1], new JObject { ["model_loaded"] = true,
                    ["native_decoder_verified"] = true, ["microphone_verified"] = false,
                    ["speech_accuracy_verified"] = false }.ToString());
                Shutdown(0);
            }
            catch { Shutdown(1); }
            return;
        }
        base.OnStartup(e);
    }
}
