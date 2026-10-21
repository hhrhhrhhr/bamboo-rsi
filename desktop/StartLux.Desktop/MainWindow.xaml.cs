using System.ComponentModel;
using System.Windows;
namespace StartLux.Desktop;
public partial class MainWindow : Window
{
    private readonly MainViewModel _model = new();
    public MainWindow()
    {
        InitializeComponent(); DataContext = _model;
        Closing += OnClosing;
    }
    // PasswordBox intentionally does not expose its password via a bindable property.
    private void CredentialChanged(object sender, RoutedEventArgs e) => _model.StoreCredential(CredentialBox.Password);
    private async void OnClosing(object? sender, CancelEventArgs e)
    {
        if (_model.Closed) return;
        e.Cancel = true;
        await _model.CloseAsync();
        Close();
    }
}
