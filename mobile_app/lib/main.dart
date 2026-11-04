import 'package:flutter/material.dart';

import 'screens/booking_screen.dart';
import 'services/api_service.dart';

void main() {
  runApp(const ResQRouteApp());
}

class ResQRouteApp extends StatelessWidget {
  const ResQRouteApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'ResQRoute',
      debugShowCheckedModeBanner: false,
      theme: ThemeData(
        colorScheme: ColorScheme.fromSeed(seedColor: const Color(0xFFE5484D)),
        useMaterial3: true,
        inputDecorationTheme: const InputDecorationTheme(border: OutlineInputBorder()),
      ),
      home: BookingScreen(api: ApiService()),
    );
  }
}
