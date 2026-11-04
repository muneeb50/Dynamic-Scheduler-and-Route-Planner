import 'package:latlong2/latlong.dart';

double? _toDouble(dynamic v) => v == null ? null : (v as num).toDouble();

List<LatLng> _points(dynamic raw) => (raw as List<dynamic>? ?? const [])
    .map((p) => LatLng(((p as List<dynamic>)[0] as num).toDouble(), (p[1] as num).toDouble()))
    .toList();

class Hospital {
  final String id;
  final String name;
  final LatLng position;

  Hospital({required this.id, required this.name, required this.position});

  factory Hospital.fromJson(Map<String, dynamic> j) => Hospital(
        id: j['id'] as String,
        name: j['name'] as String,
        position: LatLng((j['lat'] as num).toDouble(), (j['lon'] as num).toDouble()),
      );
}

class ServiceArea {
  final String name;
  final LatLng southWest;
  final LatLng northEast;
  final List<Hospital> hospitals;

  ServiceArea({required this.name, required this.southWest, required this.northEast, required this.hospitals});

  LatLng get center => LatLng((southWest.latitude + northEast.latitude) / 2, (southWest.longitude + northEast.longitude) / 2);

  factory ServiceArea.fromJson(Map<String, dynamic> j) {
    final b = (j['bbox'] as List<dynamic>).map((v) => (v as num).toDouble()).toList();
    return ServiceArea(
      name: j['name'] as String,
      southWest: LatLng(b[0], b[1]),
      northEast: LatLng(b[2], b[3]),
      hospitals: (j['hospitals'] as List<dynamic>).map((h) => Hospital.fromJson(h as Map<String, dynamic>)).toList(),
    );
  }
}

class AmbulanceInfo {
  final String id;
  final String name;
  final String status;
  final LatLng position;
  final double speed;

  AmbulanceInfo({required this.id, required this.name, required this.status, required this.position, required this.speed});

  factory AmbulanceInfo.fromJson(Map<String, dynamic> j) => AmbulanceInfo(
        id: j['id'] as String,
        name: j['name'] as String? ?? j['id'] as String,
        status: j['status'] as String? ?? '',
        position: LatLng((j['lat'] as num).toDouble(), (j['lon'] as num).toDouble()),
        speed: _toDouble(j['speed']) ?? 0,
      );
}

/// Live state of a booking as pushed by the backend (journey_dict) or embedded in REST responses.
class JourneyState {
  final String id;
  final String status;
  final double? etaS;
  final AmbulanceInfo? ambulance;
  final List<LatLng> route;
  final LatLng? pickup;
  final String? failureReason;
  final Map<String, dynamic>? metrics;

  JourneyState({
    required this.id,
    required this.status,
    this.etaS,
    this.ambulance,
    this.route = const [],
    this.pickup,
    this.failureReason,
    this.metrics,
  });

  bool get isFinal => const {'COMPLETED', 'CANCELLED', 'FAILED', 'REJECTED'}.contains(status);

  factory JourneyState.fromJson(Map<String, dynamic> j) {
    final pickup = j['pickup'] as List<dynamic>?;
    return JourneyState(
      id: j['id'] as String,
      status: j['status'] as String,
      etaS: _toDouble(j['eta_s']),
      ambulance: j['ambulance'] == null ? null : AmbulanceInfo.fromJson(j['ambulance'] as Map<String, dynamic>),
      route: _points(j['route']),
      pickup: pickup == null ? null : LatLng((pickup[0] as num).toDouble(), (pickup[1] as num).toDouble()),
      failureReason: j['failure_reason'] as String?,
      metrics: j['metrics'] as Map<String, dynamic>?,
    );
  }
}

class Booking {
  final int id;
  final String status;
  final String hospitalId;
  final String? hospitalName;
  final LatLng pickup;
  final JourneyState? live;

  Booking({required this.id, required this.status, required this.hospitalId, this.hospitalName, required this.pickup, this.live});

  factory Booking.fromJson(Map<String, dynamic> j) => Booking(
        id: j['id'] as int,
        status: j['status'] as String,
        hospitalId: j['hospital_id'] as String,
        hospitalName: j['hospital_name'] as String?,
        pickup: LatLng((j['pickup_lat'] as num).toDouble(), (j['pickup_lon'] as num).toDouble()),
        live: j['live'] == null ? null : JourneyState.fromJson(j['live'] as Map<String, dynamic>),
      );
}

const statusLabels = <String, String>{
  'WAITING_FOR_AMBULANCE': 'All ambulances busy – you are in the queue',
  'ASSIGNED': 'Ambulance assigned',
  'EN_ROUTE_TO_PICKUP': 'Ambulance on the way to you',
  'AT_PICKUP': 'Ambulance has arrived',
  'TRANSPORTING': 'On the way to hospital',
  'COMPLETED': 'Arrived at hospital',
  'CANCELLED': 'Booking cancelled',
  'FAILED': 'Journey could not be completed',
  'REJECTED': 'Request rejected',
};

String formatEta(double? s) {
  if (s == null) return '–';
  final m = s ~/ 60;
  final r = (s % 60).round();
  return m > 0 ? '${m}m ${r.toString().padLeft(2, '0')}s' : '${r}s';
}
