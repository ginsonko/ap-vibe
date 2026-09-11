import { LAYOUT_V2 } from './OfficeLayout';
import floorConfig from './office-floors.json';
import {useId} from 'react';

// ============================================================
// Cozy 16-bit pixel office diorama
// - Top-down / light isometric read
// - Keeps every workstation/bed anchor from office-layout-v2.json
// - Floor-specific decorations driven by office-floors.json
// ============================================================

const PALETTE = {
  // structure
  wall: '#8f7e6b',
  wallDark: '#6b5d4d',
  wallLight: '#b5a996',
  baseboard: '#e8dec8',
  floorWoodLight: '#f2e6d4',
  floorWoodDark: '#e9dcc6',
  // zone tints
  zoneFill: {
    management: '#e8f0ec',
    library: '#e8eef5',
    engineering: '#efe8f5',
    review: '#f5e8ec',
    collaboration: '#e8f2ec',
    waiting: '#f5f0e4',
    dormitory: '#e8f0e8',
    hub: '#f0f0f0',
  },
  // furniture
  wood: '#8c6f4a',
  woodDark: '#5c4730',
  woodLight: '#a88b66',
  metal: '#7a7a7a',
  screen: '#1a2e2e',
  screenGlow: '#3a8a8a',
  mug: '#d05a4a',
  plantPot: '#c06e45',
  plantLeaf: '#5fa855',
  bedSheet: '#eaddcf',
  blanket: '#9b7aa8',
  pillow: '#f7f0e6',
};

const WALL_THICK = 10;
const INSET = 8;

// -----------------------------------------------------------------
// Geometry helpers
// -----------------------------------------------------------------
const dirVec = {
  up: [0, -1],
  down: [0, 1],
  left: [-1, 0],
  right: [1, 0],
};

function facingDir(facing) {
  return dirVec[facing] || [0, 1];
}

function opposite(facing) {
  const map = { up: 'down', down: 'up', left: 'right', right: 'left' };
  return map[facing] || 'down';
}

function monitorPosition(facing, dist = 16) {
  const [dx, dy] = facingDir(facing);
  return { x: -dx * dist, y: -dy * dist };
}

function chairOffset(facing, dist = 22) {
  const [dx, dy] = facingDir(facing);
  return { x: dx * dist, y: dy * dist };
}

// -----------------------------------------------------------------
// Props
// -----------------------------------------------------------------
function Keyboard({ x, y, width = 24, color = '#4a4a4a' }) {
  return (
    <g transform={`translate(${x}, ${y})`}>
      <rect x={-width / 2} y="-3" width={width} height="8" rx="1" fill={color} opacity="0.9" />
      {[...Array(5)].map((_, i) => (
        <rect key={i} x={-width / 2 + 2 + i * 4} y="-1" width="3" height="3" fill="#6a6a6a" />
      ))}
    </g>
  );
}

function Mouse({ x, y, color = '#3a3a3a' }) {
  return (
    <g transform={`translate(${x}, ${y})`}>
      <rect x="-4" y="-5" width="8" height="10" rx="3" fill={color} />
      <line x1="0" y1="-5" x2="0" y2="0" stroke="#5a5a5a" strokeWidth="1" />
    </g>
  );
}

function Mug({ x, y, color = PALETTE.mug }) {
  return (
    <g transform={`translate(${x}, ${y})`}>
      <rect x="-5" y="-6" width="10" height="12" rx="2" fill={color} />
      <path d="M 5 -2 Q 9 -2 9 2 Q 9 6 5 6" fill="none" stroke={color} strokeWidth="2" />
      <circle cx="0" cy="-8" r="3" fill="#ffffff" opacity="0.4" />
    </g>
  );
}

function StickyNotes({ x, y }) {
  const colors = ['#f4d06f', '#7fd3f7', '#9bf7c0'];
  return (
    <g transform={`translate(${x}, ${y})`}>
      {colors.map((c, i) => (
        <rect key={i} x={-8 + i * 6} y={-6 + i * 2} width="6" height="8" fill={c} opacity="0.9" />
      ))}
    </g>
  );
}

function Monitor({ x, y, facing, contentColor = PALETTE.screenGlow }) {
  // Screen is a small panel on the desk back edge, facing the user.
  const isVert = facing === 'up' || facing === 'down';
  const w = isVert ? 30 : 4;
  const h = isVert ? 4 : 24;
  const rot = { up: 0, down: 0, left: 0, right: 0 }[facing] || 0;
  return (
    <g transform={`translate(${x}, ${y}) rotate(${rot})`}>
      <rect x={-w / 2} y={-h / 2} width={w} height={h} fill={PALETTE.woodDark} rx="1" />
      <rect x={-w / 2 + 1} y={-h / 2 + 1} width={w - 2} height={h - 2} fill={PALETTE.screen} rx="1" />
      <rect x={-w / 2 + 3} y={-h / 2 + 3} width={w - 6} height={h - 6} fill={contentColor} opacity="0.6" rx="1" />
      {isVert && (
        <>
          <rect x={-w / 2 + 5} y={-h / 2 + 4} width={w - 10} height="1" fill="#ffffff" opacity="0.5" />
          <rect x={-w / 2 + 5} y={-h / 2 + 6} width={(w - 10) * 0.6} height="1" fill="#ffffff" opacity="0.4" />
        </>
      )}
    </g>
  );
}

// -----------------------------------------------------------------
// Furniture
// -----------------------------------------------------------------
function Desk({ x, y, facing = 'down', label }) {
  const { x: mx, y: my } = monitorPosition(facing, 14);
  // Front edge offset (keyboard etc.)
  const { x: fx, y: fy } = chairOffset(facing, 8);
  const { x: kx, y: ky } = chairOffset(facing, 2);
  const isVert = facing === 'up' || facing === 'down';
  return (
    <g transform={`translate(${x}, ${y})`}>
      {/* shadow */}
      <rect x="-32" y="-20" width="64" height="42" fill="#000000" opacity="0.08" rx="3" />
      {/* legs */}
      <rect x="-26" y="-16" width="5" height="36" fill={PALETTE.woodDark} />
      <rect x="21" y="-16" width="5" height="36" fill={PALETTE.woodDark} />
      {/* desktop */}
      <rect x="-30" y="-18" width="60" height="36" fill={PALETTE.wood} stroke={PALETTE.woodDark} strokeWidth="1.5" rx="2" />
      <rect x="-28" y="-16" width="56" height="32" fill={PALETTE.woodLight} opacity="0.35" rx="1" />
      {/* monitor */}
      <Monitor x={mx} y={my} facing={facing} />
      {/* keyboard/mouse/mug/stickies placed on front edge */}
      <g transform={`translate(${fx}, ${fy})`}>
        <Keyboard x={isVert ? -8 : 0} y={isVert ? 0 : -8} width={isVert ? 24 : 14} />
        <Mouse x={isVert ? 12 : 0} y={isVert ? 0 : 8} />
        <Mug x={isVert ? -16 : 8} y={isVert ? 0 : -10} />
        <StickyNotes x={isVert ? 8 : -8} y={isVert ? 0 : 8} />
      </g>
      {/* tiny label under desk */}
      {label && (
        <text x="0" y="34" fontSize="8" fill="#5a5a5a" textAnchor="middle" fontFamily="ui-sans-serif, system-ui, sans-serif">
          {label}
        </text>
      )}
    </g>
  );
}

function Seat({ x, y, facing = 'down' }) {
  const rot = { up: 0, down: 180, left: 270, right: 90 }[facing] || 0;
  return (
    <g transform={`translate(${x}, ${y}) rotate(${rot})`}>
      {/* shadow */}
      <ellipse cx="0" cy="3" rx="16" ry="10" fill="#000000" opacity="0.08" />
      {/* legs */}
      <line x1="-10" y1="0" x2="-10" y2="14" stroke={PALETTE.metal} strokeWidth="2" />
      <line x1="10" y1="0" x2="10" y2="14" stroke={PALETTE.metal} strokeWidth="2" />
      {/* seat */}
      <rect x="-14" y="-8" width="28" height="18" rx="3" fill="#6b8c5e" stroke="#4a6b40" strokeWidth="1" />
      {/* backrest */}
      <rect x="-14" y="-22" width="28" height="14" rx="2" fill="#6b8c5e" stroke="#4a6b40" strokeWidth="1" />
      {/* caster wheels */}
      <circle cx="-10" cy="14" r="2" fill="#3a3a3a" />
      <circle cx="10" cy="14" r="2" fill="#3a3a3a" />
    </g>
  );
}

function Terminal({ x, y, facing = 'down' }) {
  const { x: sx, y: sy } = monitorPosition(facing, 10);
  return (
    <g transform={`translate(${x}, ${y})`}>
      {/* stand */}
      <rect x="-18" y="-10" width="36" height="22" fill="#5a5a5a" rx="3" />
      <rect x="-16" y="-8" width="32" height="18" fill="#6e6e6e" rx="2" />
      {/* screen panel */}
      <g transform={`translate(${sx}, ${sy})`}>
        <rect x="-18" y="-24" width="36" height="20" fill={PALETTE.screen} stroke="#0a2d2d" strokeWidth="1.5" rx="2" />
        <rect x="-15" y="-22" width="30" height="16" fill={PALETTE.screenGlow} opacity="0.35" rx="1" />
        <rect x="-13" y="-20" width="20" height="1.5" fill="#ffffff" opacity="0.4" />
        <rect x="-13" y="-17" width="12" height="1.5" fill="#ffffff" opacity="0.3" />
      </g>
      {/* status light */}
      <circle cx="12" cy="6" r="2.5" fill="#4ade80" opacity="0.9" />
    </g>
  );
}

function Bed({ x, y, facing = 'right' }) {
  const rot = facing === 'left' ? 180 : 0;
  return (
    <g transform={`translate(${x}, ${y}) rotate(${rot})`}>
      {/* frame */}
      <rect x="-36" y="-20" width="72" height="40" fill={PALETTE.wood} stroke={PALETTE.woodDark} strokeWidth="1.5" rx="3" />
      {/* mattress */}
      <rect x="-33" y="-17" width="66" height="34" fill={PALETTE.bedSheet} stroke="#d4c4b0" strokeWidth="1" rx="2" />
      {/* pillow */}
      <rect x="-28" y="-13" width="18" height="26" fill={PALETTE.pillow} stroke="#e0d8c8" strokeWidth="1" rx="2" />
      {/* blanket */}
      <path d="M 6 -16 L 30 -14 L 30 14 L 6 16 Z" fill={PALETTE.blanket} opacity="0.85" />
      <path d="M 6 -16 L 30 -14 L 28 -6 L 4 -8 Z" fill="#7a5e84" opacity="0.5" />
      {/* little bedside slipper */}
      <g transform="translate(38, 10)">
        <rect x="0" y="0" width="8" height="14" rx="2" fill="#8c6f4a" />
      </g>
    </g>
  );
}

function Bookshelf({ x, y, width = 70, height = 100 }) {
  return (
    <g transform={`translate(${x}, ${y})`}>
      <rect x="0" y="0" width={width} height={height} fill={PALETTE.wood} stroke={PALETTE.woodDark} strokeWidth="2" rx="2" />
      {[0.25, 0.5, 0.75].map((r, i) => (
        <rect key={i} x="2" y={height * r - 1} width={width - 4} height="3" fill={PALETTE.woodDark} />
      ))}
      {/* books */}
      {[0.12, 0.37, 0.62, 0.87].map((r, shelf) => (
        <g key={shelf}>
          {[0, 1, 2, 3].map((b) => {
            const bw = 10 + (b % 2) * 4;
            const bx = 6 + b * 15;
            const colors = ['#c8504a', '#4a8bc8', '#7ac850', '#c89050'];
            return (
              <rect
                key={b}
                x={bx}
                y={height * r}
                width={bw}
                height="16"
                fill={colors[b]}
                stroke="#2a2a2a"
                strokeWidth="0.5"
                rx="1"
              />
            );
          })}
        </g>
      ))}
      {/* top plant */}
      <Plant x={width / 2} y={-6} size="small" />
    </g>
  );
}

function Window({ x, y, width = 80, height = 60 }) {
  return (
    <g transform={`translate(${x}, ${y})`}>
      <defs>
        <linearGradient id="glass" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#dff0ff" stopOpacity="0.6" />
          <stop offset="1" stopColor="#b8d8f0" stopOpacity="0.5" />
        </linearGradient>
      </defs>
      <rect x="0" y="0" width={width} height={height} fill="#9bbcd0" stroke="#5a7080" strokeWidth="3" rx="3" />
      <rect x="3" y="3" width={width / 2 - 5} height={height / 2 - 5} fill="url(#glass)" stroke="#7a8fa0" strokeWidth="1" />
      <rect x={width / 2 + 2} y="3" width={width / 2 - 5} height={height / 2 - 5} fill="url(#glass)" stroke="#7a8fa0" strokeWidth="1" />
      <rect x="3" y={height / 2 + 2} width={width / 2 - 5} height={height / 2 - 5} fill="url(#glass)" stroke="#7a8fa0" strokeWidth="1" />
      <rect x={width / 2 + 2} y={height / 2 + 2} width={width / 2 - 5} height={height / 2 - 5} fill="url(#glass)" stroke="#7a8fa0" strokeWidth="1" />
      {/* muntins */}
      <line x1={width / 2} y1="0" x2={width / 2} y2={height} stroke="#5a7080" strokeWidth="2" />
      <line x1="0" y1={height / 2} x2={width} y2={height / 2} stroke="#5a7080" strokeWidth="2" />
    </g>
  );
}

function Plant({ x, y, size = 'small' }) {
  const scale = size === 'large' ? 1.6 : 1;
  return (
    <g transform={`translate(${x}, ${y}) scale(${scale})`}>
      <ellipse cx="0" cy="8" rx="10" ry="4" fill="#000000" opacity="0.08" />
      <path d="M -8,6 L -6,18 L 6,18 L 8,6 Z" fill={PALETTE.plantPot} stroke="#8f5232" strokeWidth="1" />
      <ellipse cx="0" cy="6" rx="8" ry="3" fill="#4a3528" />
      <ellipse cx="-4" cy="-4" rx="6" ry="11" fill={PALETTE.plantLeaf} opacity="0.9" />
      <ellipse cx="4" cy="-2" rx="5" ry="9" fill="#6fc060" opacity="0.85" />
      <ellipse cx="0" cy="-8" rx="5" ry="10" fill="#7fd070" opacity="0.95" />
    </g>
  );
}

function Door({ x, y, open = false }) {
  return (
    <g transform={`translate(${x}, ${y})`}>
      {open ? (
        <>
          <rect x="-4" y="-28" width="8" height="56" fill={PALETTE.wood} stroke={PALETTE.woodDark} strokeWidth="1" rx="1" />
          <rect x="-2" y="-26" width="4" height="52" fill={PALETTE.woodLight} opacity="0.4" />
        </>
      ) : (
        <>
          <rect x="-20" y="-30" width="40" height="60" fill={PALETTE.wood} stroke={PALETTE.woodDark} strokeWidth="2" rx="2" />
          <circle cx="12" cy="0" r="3" fill="#5a5a5a" />
        </>
      )}
    </g>
  );
}

function Rug({ x, y, width, height, color }) {
  return (
    <g transform={`translate(${x}, ${y})`}>
      <rect x={-width / 2} y={-height / 2} width={width} height={height} fill={color} opacity="0.55" rx="4" />
      <rect x={-width / 2 + 4} y={-height / 2 + 4} width={width - 8} height={height - 8} fill="none" stroke="#ffffff" strokeWidth="1" strokeDasharray="4,4" opacity="0.4" />
    </g>
  );
}

function Nightstand({ x, y }) {
  return (
    <g transform={`translate(${x}, ${y})`}>
      <rect x="-12" y="-10" width="24" height="22" fill={PALETTE.wood} stroke={PALETTE.woodDark} strokeWidth="1" rx="1" />
      <rect x="-10" y="-2" width="20" height="4" fill={PALETTE.woodDark} opacity="0.4" />
      <circle cx="0" cy="-14" r="3" fill="#f4d06f" opacity="0.9" />
    </g>
  );
}

function BulletinBoard({ x, y, width = 70, height = 50 }) {
  return (
    <g transform={`translate(${x}, ${y})`}>
      <rect x={-width / 2} y={-height / 2} width={width} height={height} fill="#bfa87a" stroke="#8c7b5a" strokeWidth="2" rx="2" />
      <rect x={-width / 2 + 3} y={-height / 2 + 3} width={width - 6} height={height - 6} fill="#f5f0e0" opacity="0.7" />
      <StickyNotes x={-10} y={-5} />
      <StickyNotes x={12} y={6} />
    </g>
  );
}

function WaterCooler({ x, y }) {
  return (
    <g transform={`translate(${x}, ${y})`}>
      <rect x="-10" y="-14" width="20" height="28" fill="#4a90a8" stroke="#2e5c6b" strokeWidth="1" rx="3" />
      <rect x="-7" y="-10" width="14" height="18" fill="#dff4fc" opacity="0.5" />
      <rect x="-2" y="-14" width="4" height="5" fill="#2e5c6b" />
      <ellipse cx="0" cy="14" rx="10" ry="3" fill="#2e5c6b" opacity="0.3" />
    </g>
  );
}

function WallClock({ x, y }) {
  return (
    <g transform={`translate(${x}, ${y})`}>
      <circle r="10" fill="#f7f0e6" stroke="#5c4730" strokeWidth="2" />
      <line x1="0" y1="0" x2="0" y2="-5" stroke="#5c4730" strokeWidth="1.5" />
      <line x1="0" y1="0" x2="4" y2="2" stroke="#5c4730" strokeWidth="1.5" />
    </g>
  );
}

// -----------------------------------------------------------------
// Wall / zone rendering
// -----------------------------------------------------------------
function zoneDoorOpening(zone, door) {
  const { x, y, width, height } = zone.bounds;
  const near = (a, b) => Math.abs(a - b) < 4;
  if (near(door.x, x)) {
    return { x: door.x - WALL_THICK / 2, y: door.y - 26, width: WALL_THICK + 4, height: 52 };
  }
  if (near(door.x, x + width)) {
    return { x: door.x - WALL_THICK / 2 - 2, y: door.y - 26, width: WALL_THICK + 4, height: 52 };
  }
  if (near(door.y, y)) {
    return { x: door.x - 26, y: door.y - WALL_THICK / 2, width: 52, height: WALL_THICK + 4 };
  }
  if (near(door.y, y + height)) {
    return { x: door.x - 26, y: door.y - WALL_THICK / 2 - 2, width: 52, height: WALL_THICK + 4 };
  }
  // fallback
  return { x: door.x - 26, y: door.y - WALL_THICK / 2, width: 52, height: WALL_THICK + 4 };
}

function ZoneWalls({ zone, tint }) {
  const { x, y, width, height } = zone.bounds;
  const floorX = x + INSET;
  const floorY = y + INSET;
  const floorW = width - INSET * 2;
  const floorH = height - INSET * 2;
  return (
    <g className={`zone-walls zone-${zone.id}`}>
      {/* inner floor tint */}
      <rect
        x={floorX}
        y={floorY}
        width={floorW}
        height={floorH}
        fill={tint || PALETTE.zoneFill[zone.id] || PALETTE.zoneFill.hub}
        opacity="0.55"
        rx="4"
      />
      {/* baseboard inside */}
      <rect
        x={floorX - 2}
        y={floorY - 2}
        width={floorW + 4}
        height={floorH + 4}
        fill="none"
        stroke={PALETTE.baseboard}
        strokeWidth="3"
        rx="5"
      />
      {/* outer wall */}
      <rect
        x={x}
        y={y}
        width={width}
        height={height}
        fill="none"
        stroke={PALETTE.wall}
        strokeWidth={WALL_THICK}
        rx="4"
      />
      {/* wall top highlight */}
      <rect
        x={x}
        y={y}
        width={width}
        height={height}
        fill="none"
        stroke={PALETTE.wallLight}
        strokeWidth="2"
        rx="4"
        opacity="0.5"
      />
      {/* door openings masks */}
      {(zone.doors || []).map((door, i) => {
        const r = zoneDoorOpening(zone, door);
        return <rect key={i} x={r.x} y={r.y} width={r.width} height={r.height} fill="url(#floor-pattern)" />;
      })}
      {/* actual door leaves */}
      {(zone.doors || []).map((door, i) => (
        <Door key={i} x={door.x} y={door.y} open={true} />
      ))}
    </g>
  );
}

// -----------------------------------------------------------------
// Main scene
// -----------------------------------------------------------------
export function OfficeSceneSVG({ floor = 0 }) {
  const patternId=useId();
  const { scene, zones } = LAYOUT_V2;
  const currentFloor = floorConfig.floors.find(f => f.id === floor) || floorConfig.floors[0];
  const currentDecor = currentFloor;
  const palette=currentFloor.palette||{};

  return (
    <svg
      viewBox={`0 0 ${scene.width} ${scene.height}`}
      className="office-scene-svg"
      style={{ width: '100%', height: '100%', maxWidth: scene.width, maxHeight: scene.height }}
      shapeRendering="crispEdges"
    >
      <defs>
        <pattern id={patternId} x="0" y="0" width="48" height="48" patternUnits="userSpaceOnUse">
          <rect x="0" y="0" width="48" height="48" fill={palette.floor||PALETTE.floorWoodLight} />
          <rect x="0" y="0" width="24" height="24" fill={palette.tile||PALETTE.floorWoodDark} opacity="0.25" />
          <rect x="24" y="24" width="24" height="24" fill={palette.tile||PALETTE.floorWoodDark} opacity="0.25" />
          <line x1="0" y1="0" x2="48" y2="0" stroke="#d6cbb8" strokeWidth="1" />
          <line x1="0" y1="0" x2="0" y2="48" stroke="#d6cbb8" strokeWidth="1" />
        </pattern>
        <linearGradient id="window-light" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#ffffff" stopOpacity="0.25" />
          <stop offset="1" stopColor="#ffffff" stopOpacity="0" />
        </linearGradient>
      </defs>

      {/* global floor */}
      <rect x="0" y="0" width={scene.width} height={scene.height} fill={`url(#${patternId})`} />

      {/* zone walls and floor tints */}
      {zones.map((zone) => (
        zone.enclosure==='open'?<g key={`wall-${zone.id}`}><rect x={zone.bounds.x} y={zone.bounds.y} width={zone.bounds.width} height={zone.bounds.height} rx="18" fill={palette.zones?.[zone.id]||PALETTE.zoneFill[zone.id]||'#e8ede5'} opacity="0.65"/><path d={`M ${zone.bounds.x+12} ${zone.bounds.y+zone.bounds.height} h ${zone.bounds.width-24}`} stroke="#d1c4ae" strokeWidth="3"/></g>:<ZoneWalls key={`wall-${zone.id}`} zone={zone} tint={palette.zones?.[zone.id]} />
      ))}

      {/* rugs */}
      {currentDecor.rugs.map((rug, i) => (
        <Rug key={`rug-${i}`} x={rug.x} y={rug.y} width={rug.width} height={rug.height} color={rug.color} />
      ))}

      {/* decorative zone details (floor-specific, non-interactive) */}
      {currentDecor.details.map((detail, i) => {
        switch (detail.type) {
          case 'bulletin':
            return <BulletinBoard key={`detail-${i}`} x={detail.x} y={detail.y} width={detail.width} height={detail.height} />;
          case 'clock':
            return <WallClock key={`detail-${i}`} x={detail.x} y={detail.y} />;
          case 'plant':
            return <Plant key={`detail-${i}`} x={detail.x} y={detail.y} size={detail.size} />;
          case 'bookshelf':
            return <Bookshelf key={`detail-${i}`} x={detail.x} y={detail.y} width={detail.width} height={detail.height} />;
          case 'window':
            return <Window key={`detail-${i}`} x={detail.x} y={detail.y} width={detail.width} height={detail.height} />;
          case 'windowLight':
            return (
              <path
                key={`detail-${i}`}
                d={`M ${detail.x1},${detail.y1} L ${detail.x2},${detail.y2} L ${detail.x3},${detail.y3} L ${detail.x4},${detail.y4} Z`}
                fill="url(#window-light)"
                opacity="0.4"
                pointerEvents="none"
              />
            );
          case 'waterCooler':
            return <WaterCooler key={`detail-${i}`} x={detail.x} y={detail.y} />;
          case 'equipmentRack':
            return (
              <g key={`detail-${i}`}>
                <rect x={detail.x} y={detail.y} width={detail.width} height={detail.height} fill="#4a4a4a" stroke="#2a2a2a" strokeWidth="2" rx="3" />
                <rect x={detail.x + 3} y={detail.y + 3} width={detail.width - 6} height="8" fill="#5a5a5a" rx="1" />
                <rect x={detail.x + 3} y={detail.y + 14} width={detail.width - 6} height="8" fill="#5a5a5a" rx="1" />
                <rect x={detail.x + 3} y={detail.y + 25} width={detail.width - 6} height="8" fill="#5a5a5a" rx="1" />
                <circle cx={detail.x + detail.width - 5} cy={detail.y + 20} r="3" fill="#4ade80" opacity="0.8" />
              </g>
            );
          case 'collabTable':
            return (
              <g key={`detail-${i}`}>
                <rect x={detail.x} y={detail.y} width={detail.width} height={detail.height} rx="4" fill={PALETTE.wood} stroke={PALETTE.woodDark} strokeWidth="1.5" />
                <rect x={detail.x + 2} y={detail.y + 2} width={detail.width - 4} height={detail.height - 4} rx="2" fill={PALETTE.woodLight} opacity="0.4" />
              </g>
            );
          case 'nightstand':
            return <Nightstand key={`detail-${i}`} x={detail.x} y={detail.y} />;
          default:
            return null;
        }
      })}

      {/* furniture from layout anchors */}
      {zones.map((zone) => (
        <g key={`furniture-${zone.id}`}>
          {zone.workstations?.map((ws) => {
            switch (ws.type) {
              case 'desk':
                return <Desk key={ws.id} x={ws.x} y={ws.y} facing={ws.facing} label={ws.label} />;
              case 'seat':
                return <Seat key={ws.id} x={ws.x} y={ws.y} facing={ws.facing} />;
              case 'terminal':
                return <Terminal key={ws.id} x={ws.x} y={ws.y} facing={ws.facing} />;
              default:
                return null;
            }
          })}
          {zone.beds?.map((bed) => (
            <Bed key={bed.id} x={bed.x} y={bed.y} facing={bed.facing} />
          ))}
        </g>
      ))}

      {/* zone labels on top */}
      {zones.map((zone) => {
        const { x, y, width, height } = zone.bounds;
        return (
          <g key={`label-${zone.id}`}>
            <rect x={x + 8} y={y + 6} width={110} height="22" rx="4" fill="#ffffff" opacity="0.85" />
            <text
              x={x + 18}
              y={y + 21}
              fill={zone.color}
              fontSize="13"
              fontWeight="700"
              fontFamily="ui-sans-serif, system-ui, sans-serif"
            >
              {zone.display_name}
            </text>
          </g>
        );
      })}
    </svg>
  );
}
