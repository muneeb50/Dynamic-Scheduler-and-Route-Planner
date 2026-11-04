import 'package:flutter/material.dart';
import 'package:flutter_map/flutter_map.dart';
import 'package:latlong2/latlong.dart';

import '../models/models.dart';
import '../services/api_service.dart';
import '../widgets/map_widgets.dart';
import 'tracking_screen.dart';

class BookingScreen extends StatefulWidget {
  const BookingScreen({super.key, required this.api});
  final ApiService api;

  @override
  State<BookingScreen> createState() => _BookingScreenState();
}

class _BookingScreenState extends State<BookingScreen> {
  final _form = GlobalKey<FormState>();
  final _name = TextEditingController();
  final _phone = TextEditingController();
  final _notes = TextEditingController();
  final _map = MapController();
  ServiceArea? _area;
  String? _hospitalId;
  LatLng? _pickup;
  String? _error;
  bool _loading = true;
  bool _submitting = false;

  @override
  void initState() {
    super.initState();
    _loadArea();
  }

  @override
  void dispose() {
    _name.dispose();
    _phone.dispose();
    _notes.dispose();
    super.dispose();
  }

  Future<void> _loadArea() async {
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final area = await widget.api.serviceArea();
      setState(() {
        _area = area;
        _hospitalId = area.hospitals.isNotEmpty ? area.hospitals.first.id : null;
      });
    } on ApiException catch (e) {
      setState(() => _error = e.message);
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  Future<void> _submit() async {
    if (!_form.currentState!.validate()) return;
    if (_pickup == null) {
      setState(() => _error = 'Tap the map to set the pickup location.');
      return;
    }
    setState(() {
      _submitting = true;
      _error = null;
    });
    try {
      final booking = await widget.api.createBooking(
        name: _name.text.trim(),
        phone: _phone.text.trim(),
        pickup: _pickup!,
        hospitalId: _hospitalId!,
        notes: _notes.text.trim(),
      );
      if (!mounted) return;
      final hospital = _area!.hospitals.firstWhere((h) => h.id == booking.hospitalId);
      await Navigator.of(context).push(MaterialPageRoute(
        builder: (_) => TrackingScreen(api: widget.api, booking: booking, hospital: hospital),
      ));
      setState(() => _pickup = null);
    } on ApiException catch (e) {
      setState(() => _error = e.message);
    } finally {
      if (mounted) setState(() => _submitting = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final area = _area;
    return Scaffold(
      appBar: AppBar(
        title: const Text('Request an ambulance'),
        backgroundColor: const Color(0xFFE5484D),
        foregroundColor: Colors.white,
      ),
      body: _loading
          ? const Center(child: CircularProgressIndicator())
          : area == null
              ? _ErrorView(message: _error ?? 'Could not load the service area', onRetry: _loadArea)
              : Column(
                  children: [
                    SizedBox(
                      height: MediaQuery.of(context).size.height * 0.42,
                      child: Stack(
                        children: [
                          FlutterMap(
                            mapController: _map,
                            options: MapOptions(
                              initialCenter: area.center,
                              initialZoom: 14.5,
                              onTap: (_, point) => setState(() => _pickup = point),
                            ),
                            children: [
                              osmTiles(),
                              MarkerLayer(markers: [
                                for (final h in area.hospitals)
                                  pinMarker(h.position, color: const Color(0xFF0090FF), icon: Icons.local_hospital),
                                if (_pickup != null)
                                  pinMarker(_pickup!, color: const Color(0xFFE5484D), icon: Icons.person_pin_circle, size: 42),
                              ]),
                              osmAttribution(),
                            ],
                          ),
                          Positioned(
                            left: 12,
                            top: 12,
                            child: Card(
                              child: Padding(
                                padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 6),
                                child: Text(_pickup == null ? 'Tap the map to set pickup' : 'Pickup set – tap again to move'),
                              ),
                            ),
                          ),
                        ],
                      ),
                    ),
                    Expanded(
                      child: Form(
                        key: _form,
                        child: ListView(
                          padding: const EdgeInsets.all(16),
                          children: [
                            TextFormField(
                              controller: _name,
                              decoration: const InputDecoration(labelText: 'Patient name', prefixIcon: Icon(Icons.person)),
                              validator: (v) => (v == null || v.trim().isEmpty) ? 'Enter the patient name' : null,
                            ),
                            const SizedBox(height: 10),
                            TextFormField(
                              controller: _phone,
                              keyboardType: TextInputType.phone,
                              decoration: const InputDecoration(labelText: 'Phone number', prefixIcon: Icon(Icons.phone)),
                              validator: (v) {
                                final digits = (v ?? '').replaceAll(RegExp(r'[\s-]'), '');
                                return RegExp(r'^\+?\d{5,15}$').hasMatch(digits) ? null : 'Enter a valid phone number';
                              },
                            ),
                            const SizedBox(height: 10),
                            DropdownButtonFormField<String>(
                              value: _hospitalId,
                              decoration: const InputDecoration(labelText: 'Hospital', prefixIcon: Icon(Icons.local_hospital)),
                              items: [
                                for (final h in area.hospitals) DropdownMenuItem(value: h.id, child: Text(h.name)),
                              ],
                              onChanged: (v) => setState(() => _hospitalId = v),
                              validator: (v) => v == null ? 'Choose a hospital' : null,
                            ),
                            const SizedBox(height: 10),
                            TextFormField(
                              controller: _notes,
                              decoration: const InputDecoration(labelText: 'Notes for the crew (optional)', prefixIcon: Icon(Icons.notes)),
                              maxLength: 500,
                            ),
                            if (_error != null)
                              Padding(
                                padding: const EdgeInsets.only(bottom: 8),
                                child: Text(_error!, style: const TextStyle(color: Colors.redAccent)),
                              ),
                            FilledButton.icon(
                              style: FilledButton.styleFrom(
                                backgroundColor: const Color(0xFFE5484D),
                                padding: const EdgeInsets.symmetric(vertical: 16),
                              ),
                              onPressed: _submitting ? null : _submit,
                              icon: _submitting
                                  ? const SizedBox(width: 18, height: 18, child: CircularProgressIndicator(strokeWidth: 2, color: Colors.white))
                                  : const Icon(Icons.emergency),
                              label: const Text('Request ambulance', style: TextStyle(fontSize: 16)),
                            ),
                            const SizedBox(height: 8),
                            Text(
                              'Prototype: ambulances and traffic are simulated in ${area.name}.',
                              style: Theme.of(context).textTheme.bodySmall,
                              textAlign: TextAlign.center,
                            ),
                          ],
                        ),
                      ),
                    ),
                  ],
                ),
    );
  }
}

class _ErrorView extends StatelessWidget {
  const _ErrorView({required this.message, required this.onRetry});
  final String message;
  final VoidCallback onRetry;

  @override
  Widget build(BuildContext context) => Center(
        child: Padding(
          padding: const EdgeInsets.all(24),
          child: Column(mainAxisSize: MainAxisSize.min, children: [
            const Icon(Icons.wifi_off, size: 48, color: Colors.grey),
            const SizedBox(height: 12),
            Text(message, textAlign: TextAlign.center),
            const SizedBox(height: 12),
            OutlinedButton(onPressed: onRetry, child: const Text('Retry')),
          ]),
        ),
      );
}
