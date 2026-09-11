import layout from './office-layout-v2.json';

export const LAYOUT_V2 = layout;
export const getAllWorkstations = () => layout.zones.flatMap(zone => (zone.workstations || []).map(station=>({...station,zone_id:zone.id,room_id:zone.room_id})));
export const getAllBeds = () => layout.zones.flatMap(zone => (zone.beds || []).map(station=>({...station,type:'bed',zone_id:zone.id,room_id:zone.room_id})));
export const getZonesByRoomId = room => layout.zones.filter(zone=>zone.room_id===room);
const stations = [...getAllWorkstations(),...getAllBeds()];
// A station has one occupant. Overflow stays in the list and other map pages.
export function workstationSlots(members,page=0) {
  const slots=[];
  for (const room of new Set(stations.map(s=>s.room_id))) {
    const seats=stations.filter(s=>s.room_id===room);
    const occupants=members.filter(m=>m.room===room).sort((a,b)=>Number(!!b.active)-Number(!!a.active)||a.agent.agent_id.localeCompare(b.agent.agent_id));
    occupants.slice(page*seats.length,(page+1)*seats.length).forEach((member,i)=>{
      const station=seats[i];slots.push({member,room,station,point:station.actor || {x:station.x,y:station.y+24}});
    });
  }
  return slots;
}
export function mapPageCount(members) {
  return Math.max(1,...[...new Set(stations.map(s=>s.room_id))].map(room=>Math.ceil(members.filter(m=>m.room===room).length/stations.filter(s=>s.room_id===room).length)));
}
