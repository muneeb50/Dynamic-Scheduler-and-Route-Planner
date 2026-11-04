import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_map/flutter_map.dart';
import 'package:latlong2/latlong.dart';

import '../models/models.dart';
import '../services/api_service.dart';
import '../services/tracking_service.dart';
import '../widgets/map_widgets.dart';

class TrackingScreen extends StatefulWidget {
  const TrackingScreen({super.key, required this.api, required this.booking, required this.hospital});
  final ApiService api;
  final Booking booking;
  final Hospital hospital;

  @override
  State<TrackingScreen> createState() => _TrackingScreenState();
}

class _TrackingScreenState extends State<TrackingScreen> {
  late final TrackingService _tracking;
  StreamSubscription<TrackingUpdate>? _sub;
  final _map = MapController();
  JourneyState? _journey;
  LinkStatus _link = LinkStatus.connecting;
  bool _follow = true;
  bool _cancelling = false;

  @override
  void initState() {
    super.initState();
    _journey = widget.booking.live;
    _tracking = TrackingService(widget.booking.id, widget.api);
    _sub = _tracking.updates.listen((u) {
      setState(() {
        _journey = u.journey ?? _journey;
        _link = u.link;
      });
      final amb = _journey?.ambulance;
      if (_follow && amb != null && !(_journey?.isFinal ?? false)) {
        _moveTo(amb.position);
      }
    });
    _tracking.start();
  }

  void _moveTo(LatLng p) {
    try {
      _map.move(p, _map.camera.zoom);
    } catch (_) {
      // the map has not been laid out yet (first update can arrive before the first frame)
    }
  }

  @override
  void dispose() {
    _sub?.cancel();
    _tracking.dispose();
    super.dispose();
  }

  Future<void> _cancel() async {
    final ok = await showDialog<bool>(
      context: context,
      builder: (c) => AlertDialog(
        title: const Text('Cancel this booking?'),
        content: const Text('The ambulance will be released for other emergencies.'),
        actions: [
          TextButton(onPressed: () => Navigator.pop(c, false), child: const Text('Keep')),
          FilledButton(onPressed: () => Navigator.pop(c, true), child: const Text('Cancel booking')),
        ],
      ),
    );
    if (ok != true) return;
    setState(() => _cancelling = true);
    try {
      await widget.api.cancelBooking(widget.booking.id);
    } on ApiException catch (e) {
      if (mounted) ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(e.message)));
    } finally {
      if (mounted) setState(() => _cancelling = false);
    }
  }

  Widget _linkChip() => switch (_link) {
        LinkStatus.live => const LinkChip(label: 'Live', color: Colors.green),
        LinkStatus.stale => const LinkChip(label: 'Tracking delayed', color: Colors.orange),
        LinkStatus.reconnecting => const LinkChip(label: 'Reconnecting…', color: Colors.orange),
        LinkStatus.connecting => const LinkChip(label: 'Connecting…', color: Colors.grey),
      };

  @override
  Widget build(BuildContext context) {
    final j = _journey;
    final status = j?.status ?? widget.booking.status;
    final amb = j?.ambulance;
    final pickup = j?.pickup ?? widget.booking.pickup;
    final finished = j?.isFinal ?? false;
    final etaText = status == 'COMPLETED'
        ? 'Arrived'
        : status == 'AT_PICKUP'
            ? 'Boarding'
            : formatEta(j?.etaS);

    return Scaffold(
      appBar: AppBar(
        title: Text('Booking #${widget.booking.id}'),
        backgroundColor: const Color(0xFFE5484D),
        foregroundColor: Colors.white,
        actions: [Padding(padding: const EdgeInsets.only(right: 12), child: Center(child: _linkChip()))],
      ),
      body: Column(
        children: [
          Expanded(
            child: Stack(children: [
              FlutterMap(
                mapController: _map,
                options: MapOptions(
                  initialCenter: amb?.position ?? pickup,
                  initialZoom: 15,
                  onPositionChanged: (_, hasGesture) {
                    if (hasGesture && _follow) setState(() => _follow = false);
                  },
                ),
                children: [
                  osmTiles(),
                  if (j != null && j.route.length > 1)
                    PolylineLayer(polylines: [
                      Polyline(points: j.route, strokeWidth: 5, color: const Color(0xFFE5484D)),
                    ]),
                  MarkerLayer(markers: [
                    pinMarker(widget.hospital.position, color: const Color(0xFF0090FF), icon: Icons.local_hospital),
                    pinMarker(pickup, color: const Color(0xFFF5A524), icon: Icons.person_pin_circle),
                    if (amb != null)
                      pinMarker(amb.position, color: const Color(0xFFE5484D), icon: Icons.emergency, size: 44),
                  ]),
                  osmAttribution(),
                ],
              ),
              if (!_follow && amb != null)
                Positioned(
                  right: 12,
                  bottom: 12,
                  child: FloatingActionButton.small(
                    onPressed: () {
                      setState(() => _follow = true);
                      _moveTo(amb.position);
                    },
                    child: const Icon(Icons.my_location),
                  ),
                ),
            ]),
          ),
          Material(
            elevation: 8,
            child: Padding(
              padding: const EdgeInsets.fromLTRB(20, 16, 20, 24),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(statusLabels[status] ?? status, style: Theme.of(context).textTheme.titleMedium),
                  const SizedBox(height: 4),
                  Text(etaText, style: Theme.of(context).textTheme.displaySmall?.copyWith(fontWeight: FontWeight.w800)),
                  if (status == 'TRANSPORTING' || status == 'EN_ROUTE_TO_PICKUP')
                    Text(status == 'TRANSPORTING' ? 'to ${widget.hospital.name}' : 'until the ambulance reaches you',
                        style: Theme.of(context).textTheme.bodySmall),
                  if (amb != null) ...[
                    const SizedBox(height: 8),
                    Text('${amb.name} (${amb.id}) · ${(amb.speed * 3.6).toStringAsFixed(0)} km/h'),
                  ],
                  if (_link == LinkStatus.stale || _link == LinkStatus.reconnecting)
                    const Padding(
                      padding: EdgeInsets.only(top: 6),
                      child: Text('Position may be out of date while we reconnect.', style: TextStyle(color: Colors.orange)),
                    ),
                  if (j?.failureReason != null)
                    Padding(
                      padding: const EdgeInsets.only(top: 6),
                      child: Text(j!.failureReason!, style: const TextStyle(color: Colors.redAccent)),
                    ),
                  if (j?.metrics != null && status == 'COMPLETED')
                    Padding(
                      padding: const EdgeInsets.only(top: 6),
                      child: Text(
                        '${j!.metrics!['priority_granted'] ?? 0} green lights granted · '
                        '${j.metrics!['signal_delay_s'] ?? 0}s waiting at signals',
                      ),
                    ),
                  const SizedBox(height: 12),
                  if (!finished)
                    OutlinedButton.icon(
                      onPressed: _cancelling ? null : _cancel,
                      icon: const Icon(Icons.close),
                      label: const Text('Cancel booking'),
                    )
                  else
                    FilledButton(onPressed: () => Navigator.of(context).pop(), child: const Text('Done')),
                ],
              ),
            ),
          ),
        ],
      ),
    );
  }
}
