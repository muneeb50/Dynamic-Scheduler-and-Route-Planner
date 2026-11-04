import 'dart:convert';

import 'package:http/http.dart' as http;
import 'package:latlong2/latlong.dart';

import '../config.dart';
import '../models/models.dart';

class ApiException implements Exception {
  final int? statusCode;
  final String message;
  ApiException(this.message, [this.statusCode]);
  @override
  String toString() => message;
}

class ApiService {
  ApiService({http.Client? client}) : _client = client ?? http.Client();
  final http.Client _client;
  static const _timeout = Duration(seconds: 10);

  Uri _u(String path) => Uri.parse('$apiBase$path');

  Future<dynamic> _send(Future<http.Response> req) async {
    try {
      final res = await req.timeout(_timeout);
      return _decode(res);
    } on ApiException {
      rethrow;
    } catch (_) {
      throw ApiException('Cannot reach the ResQRoute server at $apiBase. Check your connection.');
    }
  }

  dynamic _decode(http.Response res) {
    final body = res.body.isEmpty ? null : jsonDecode(res.body);
    if (res.statusCode >= 400) {
      final detail = body is Map<String, dynamic> ? body['detail'] : null;
      throw ApiException(detail is String ? detail : 'Request failed (${res.statusCode})', res.statusCode);
    }
    return body;
  }

  Future<ServiceArea> serviceArea() async =>
      ServiceArea.fromJson(await _send(_client.get(_u('/api/network'))) as Map<String, dynamic>);

  Future<Booking> createBooking({
    required String name,
    required String phone,
    required LatLng pickup,
    required String hospitalId,
    String? notes,
  }) async {
    final body = jsonEncode({
      'patient_name': name,
      'phone': phone,
      'pickup_lat': pickup.latitude,
      'pickup_lon': pickup.longitude,
      'hospital_id': hospitalId,
      if (notes != null && notes.isNotEmpty) 'notes': notes,
    });
    final json = await _send(_client.post(_u('/api/bookings'), headers: {'Content-Type': 'application/json'}, body: body));
    return Booking.fromJson(json as Map<String, dynamic>);
  }

  Future<Booking> getBooking(int id) async =>
      Booking.fromJson(await _send(_client.get(_u('/api/bookings/$id'))) as Map<String, dynamic>);

  Future<Booking> cancelBooking(int id) async =>
      Booking.fromJson(await _send(_client.post(_u('/api/bookings/$id/cancel'))) as Map<String, dynamic>);
}
