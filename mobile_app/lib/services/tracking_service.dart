import 'dart:async';
import 'dart:convert';

import 'package:web_socket_channel/web_socket_channel.dart';

import '../config.dart';
import '../models/models.dart';
import 'api_service.dart';

enum LinkStatus { connecting, live, stale, reconnecting }

class TrackingUpdate {
  final JourneyState? journey;
  final LinkStatus link;
  final DateTime? lastUpdate;
  TrackingUpdate(this.journey, this.link, this.lastUpdate);
}

/// Live booking updates over WebSocket (/ws/bookings/{id}), with
///  * automatic reconnect using exponential back-off,
///  * stale-data detection when no message arrives for [staleAfter],
///  * REST polling as a fallback while the socket is down.
class TrackingService {
  TrackingService(this.bookingId, this.api);

  final int bookingId;
  final ApiService api;
  final _controller = StreamController<TrackingUpdate>.broadcast();
  WebSocketChannel? _channel;
  StreamSubscription<dynamic>? _sub;
  Timer? _watchdog;
  Timer? _retry;
  Timer? _ping;
  int _attempt = 0;
  bool _disposed = false;
  JourneyState? _last;
  DateTime? _lastAt;
  LinkStatus _link = LinkStatus.connecting;

  Stream<TrackingUpdate> get updates => _controller.stream;

  void start() {
    _connect();
    _watchdog = Timer.periodic(const Duration(seconds: 1), (_) => _checkStale());
  }

  void _emit() {
    if (!_controller.isClosed) _controller.add(TrackingUpdate(_last, _link, _lastAt));
  }

  Future<void> _connect() async {
    if (_disposed) return;
    _link = _attempt == 0 ? LinkStatus.connecting : LinkStatus.reconnecting;
    _emit();
    try {
      final ch = WebSocketChannel.connect(Uri.parse('$wsBase/ws/bookings/$bookingId'));
      _channel = ch;
      await ch.ready;
      _attempt = 0;
      _sub = ch.stream.listen(_onMessage, onError: (_) => _scheduleReconnect(), onDone: _scheduleReconnect);
      _ping = Timer.periodic(const Duration(seconds: 10), (_) => ch.sink.add('ping'));
    } catch (_) {
      _scheduleReconnect();
    }
  }

  void _onMessage(dynamic raw) {
    final msg = jsonDecode(raw as String) as Map<String, dynamic>;
    final booking = msg['booking'];
    if (booking is Map<String, dynamic>) {
      _last = JourneyState.fromJson(booking);
    }
    _lastAt = DateTime.now();
    _link = LinkStatus.live;
    _emit();
  }

  void _scheduleReconnect() {
    if (_disposed || (_retry?.isActive ?? false)) return; // onError and onDone can both fire
    _ping?.cancel();
    _sub?.cancel();
    _channel = null;
    _attempt += 1;
    _link = LinkStatus.reconnecting;
    _emit();
    _pollOnce();
    final exponent = _attempt > 5 ? 5 : _attempt;
    final ms = 500 * (1 << exponent);
    final delay = Duration(milliseconds: ms > 10000 ? 10000 : ms);
    _retry?.cancel();
    _retry = Timer(delay, _connect);
  }

  Future<void> _pollOnce() async {
    try {
      final b = await api.getBooking(bookingId);
      if (b.live != null) {
        _last = b.live;
        _lastAt = DateTime.now();
        _emit();
      }
    } catch (_) {
      // still offline; the link status already says so
    }
  }

  void _checkStale() {
    if (_lastAt == null) return;
    if (_link == LinkStatus.live && DateTime.now().difference(_lastAt!) > staleAfter) {
      _link = LinkStatus.stale;
      _emit();
    }
  }

  void dispose() {
    _disposed = true;
    _watchdog?.cancel();
    _retry?.cancel();
    _ping?.cancel();
    _sub?.cancel();
    _channel?.sink.close();
    _controller.close();
  }
}
