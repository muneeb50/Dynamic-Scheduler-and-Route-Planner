/// Backend address. Override at run time, e.g.
///   flutter run --dart-define=API_BASE=http://192.168.1.20:8000
/// Defaults: Android emulator -> 10.0.2.2 (the host machine). For Chrome/desktop use
///   --dart-define=API_BASE=http://localhost:8000
const String apiBase = String.fromEnvironment('API_BASE', defaultValue: 'http://10.0.2.2:8000');

String get wsBase => apiBase.replaceFirst(RegExp(r'^http'), 'ws');

/// Tracking data older than this is shown as stale.
const Duration staleAfter = Duration(seconds: 4);
