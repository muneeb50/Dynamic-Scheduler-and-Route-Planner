# Flowcharts

These Mermaid diagrams render directly on GitHub and in most Markdown viewers (VS Code: "Markdown Preview Mermaid Support"). Paste them into <https://mermaid.live> to export PNG/SVG for the report or slides.

## 1. End-to-end flow

```mermaid
flowchart TD
    A([Patient opens app]) --> B[Choose pickup on map + hospital]
    B --> C[POST /api/bookings]
    C --> D{Pickup inside service area?}
    D -- no --> D1[422: reject with reason] --> Z([End])
    D -- yes --> E[Snap pickup to road network<br/>store booking]
    E --> F{Ambulance available?}
    F -- no --> F1[Status WAITING_FOR_AMBULANCE<br/>auto-assign when one frees up] --> F
    F -- yes --> G[Choose ambulance with lowest<br/>emergency-aware ETA]
    G --> H[Plan route: road graph + live queues]
    H --> I[Insert ambulance into simulation]
    I --> J[[Tracking loop: position + ETA<br/>pushed over WebSocket]]
    J --> K{Next signal on route<br/>within lead time?}
    K -- yes --> L[[Intersection algorithm<br/>see diagram 2]]
    L --> J
    K -- no --> M{Leg finished?}
    M -- no --> N{No progress / time limit?}
    N -- yes --> N1[FAILED + release priority<br/>send ambulance home] --> Z
    N -- no --> J
    M -- pickup reached --> O[Dwell at pickup] --> H2[Plan route to hospital] --> I
    M -- hospital reached --> P[COMPLETED: compute journey metrics]
    P --> Q[Ambulance returns to base → AVAILABLE]
    Q --> Z
    C -. user cancels any time .-> X[CANCELLED: release priority,<br/>remove vehicle, return to base] --> Z
```

## 2. Intersection-level algorithm (priority + recovery)

```mermaid
flowchart TD
    S([Ambulance moving]) --> A[Walk the remaining route edges<br/>find next signalised junction]
    A --> B{Within detection range?}
    B -- no --> S
    B -- yes --> C[ETA to stop line from free-flow speeds]
    C --> D[Queue ahead of ambulance<br/>sim ground truth or CV estimate]
    D --> E[lead = switch time min-green+amber+all-red<br/>+ queue discharge 2s+2s/veh + margin]
    E --> F{ETA ≤ lead<br/>or distance ≤ forced range?}
    F -- no --> S
    F -- yes --> G{Junction already serving<br/>another emergency?}
    G -- yes --> G1[PRIORITY_DEFERRED] --> S
    G -- no --> H[Record baseline queues<br/>PRIORITY_REQUESTED]
    H --> I{Ambulance's group green?}
    I -- yes --> K[Hold green]
    I -- no --> J[Wait for min green →<br/>AMBER → ALL-RED → GREEN for ambulance]
    J --> K
    K --> L{Ambulance observed on the<br/>exit edge past the junction?}
    L -- no --> T{Held longer than max hold?}
    T -- no --> K
    T -- yes --> T1[PRIORITY_TIMEOUT logged] --> R
    L -- yes --> R[Release priority<br/>start recovery]
    R --> R1[ΔQᵢ = Qᵢ now − Qᵢ baseline]
    R1 --> R2[Each new green:<br/>clamp base + gain·max ΔQ⁺/lanes, min, max]
    R2 --> R3{All ΔQᵢ within tolerance<br/>for 10 s?}
    R3 -- yes --> R4[RECOVERY_COMPLETED<br/>duration logged] --> S
    R3 -- no --> R5{Max recovery time?}
    R5 -- no --> R1
    R5 -- yes --> R6[RECOVERY_TIMEOUT<br/>residual backlog logged] --> S
```

## 3. Signal state machine (the only transitions allowed)

```mermaid
stateDiagram-v2
    [*] --> GREEN
    GREEN --> YELLOW: green time over<br/>or priority for other group after min green
    YELLOW --> ALL_RED: 3 s
    ALL_RED --> GREEN: 2 s · next group (or priority group)
    note right of GREEN
      Priority for the current group holds GREEN.
      Every tick checks that no two groups are
      green/amber together; violations are counted.
    end note
```

## 4. Booking lifecycle

```mermaid
stateDiagram-v2
    [*] --> WAITING_FOR_AMBULANCE
    WAITING_FOR_AMBULANCE --> EN_ROUTE_TO_PICKUP: ambulance assigned
    EN_ROUTE_TO_PICKUP --> AT_PICKUP
    AT_PICKUP --> TRANSPORTING: dwell finished
    TRANSPORTING --> COMPLETED
    WAITING_FOR_AMBULANCE --> CANCELLED
    EN_ROUTE_TO_PICKUP --> CANCELLED
    AT_PICKUP --> CANCELLED
    TRANSPORTING --> CANCELLED
    EN_ROUTE_TO_PICKUP --> FAILED: no progress / time limit
    TRANSPORTING --> FAILED: no progress / time limit
```

## 5. Component architecture

```mermaid
flowchart LR
    subgraph Clients
      M[Flutter patient app]
      W[React admin dashboard<br/>+ web patient page]
    end
    subgraph Backend[FastAPI backend]
      API[REST /api/*]
      WS[WebSockets /ws/*]
      LIVE[Live simulation service<br/>persists events, broadcasts]
      COORD[Coordinator]
      DISP[Dispatch + emergency-aware routing]
      PRI[Priority manager]
      REC[Recovery engine]
      SIG[Signal controllers]
      EXP[Experiment runner]
    end
    subgraph Engines[Traffic engines]
      BI[Built-in IDM simulator]
      SU[SUMO via TraCI]
    end
    CV[Computer vision<br/>render → OpenCV detect → queue estimate]
    DB[(SQLite or<br/>PostgreSQL + PostGIS)]
    NET[[data/network.json<br/>demo or OpenStreetMap]]
    M & W <--> API
    M & W <--> WS
    API --> LIVE --> COORD
    WS --- LIVE
    COORD --> DISP & PRI & REC & SIG
    COORD <--> BI
    COORD <--> SU
    COORD <--> CV
    LIVE --> DB
    EXP --> COORD
    NET --> COORD
    NET --> SU
```
