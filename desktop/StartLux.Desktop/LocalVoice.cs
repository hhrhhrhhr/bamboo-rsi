using NAudio.Wave;
using Newtonsoft.Json.Linq;
using Vosk;
namespace StartLux.Desktop;

internal sealed class LocalVoice : IDisposable
{
    private readonly Model _model;
    private readonly VoskRecognizer _recognizer;
    private readonly WaveInEvent _microphone;
    private readonly object _gate = new();
    private bool _disposed;
    public event Action<string>? Recognized;
    public event Action<string>? Faulted;

    public LocalVoice(string modelPath)
    {
        Vosk.Vosk.SetLogLevel(-1);
        _model = new Model(modelPath);
        try
        {
            _recognizer = new VoskRecognizer(_model, 16000);
            _recognizer.SetWords(true);
            _microphone = new WaveInEvent { WaveFormat = new WaveFormat(16000, 16, 1), BufferMilliseconds = 100 };
            _microphone.DataAvailable += OnAudio;
            _microphone.RecordingStopped += (_, e) => { if (e.Exception != null) Faulted?.Invoke("麦克风不可用，请检查 Windows 麦克风权限"); };
        }
        catch { _model.Dispose(); throw; }
    }
    public void Start() => _microphone.StartRecording();
    private void OnAudio(object? sender, WaveInEventArgs e)
    {
        string? text = null;
        lock (_gate)
        {
            if (_disposed || !_recognizer.AcceptWaveform(e.Buffer, e.BytesRecorded)) return;
            var result = JObject.Parse(_recognizer.Result());
            var words = result["result"] as JArray;
            if (words is { Count: > 0 } && words.Average(w => w.Value<double>("conf")) >= 0.8)
                text = result.Value<string>("text");
        }
        if (!string.IsNullOrWhiteSpace(text)) Recognized?.Invoke(text);
    }
    public void Dispose()
    {
        _microphone.StopRecording();
        lock (_gate)
        {
            if (_disposed) return;
            _disposed = true;
            _microphone.Dispose(); _recognizer.Dispose(); _model.Dispose();
        }
    }
}
