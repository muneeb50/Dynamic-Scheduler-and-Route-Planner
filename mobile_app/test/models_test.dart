import 'package:flutter_test/flutter_test.dart';
import 'package:resqroute_patient/models/models.dart';

void main() {
  test('parses a live journey update from the backend', () {
    final j = JourneyState.fromJson({
      'id': 'B7',
      'status': 'EN_ROUTE_TO_PICKUP',
      'eta_s': 95.5,
      'pickup': [19.03, 73.031],
      'route': [
        [19.03, 73.02],
        [19.03, 73.031]
      ],
      'ambulance': {'id': 'A1', 'name': 'Station West', 'status': 'EN_ROUTE_TO_PICKUP', 'lat': 19.03, 'lon': 73.02, 'speed': 12.0},
      'failure_reason': null,
      'metrics': null,
    });
    expect(j.route.length, 2);
    expect(j.ambulance!.id, 'A1');
    expect(j.isFinal, isFalse);
    expect(formatEta(j.etaS), '1m 36s');
  });
}
