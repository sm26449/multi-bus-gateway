/* A Fronius Solar API stand-in for e2e: enough of the DataManager to be
 * discovered and polled. Port from FAKE_PORT (default 18099). */
import http from 'http';
const port = parseInt(process.env.FAKE_PORT || '18099', 10);
const json = (res, body) => { const raw = JSON.stringify(body); res.writeHead(200, { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(raw) }); res.end(raw); };
http.createServer((req, res) => {
  const u = new URL(req.url, 'http://x');
  if (u.pathname.endsWith('GetActiveDeviceInfo.cgi')) return json(res, { Body: { Data: {
    Inverter: { '1': { DT: 122, Serial: 'A1' }, '2': { DT: 122, Serial: 'A2' }, '3': { DT: 99, Serial: 'A3' } },
    Meter: { '0': { Serial: 'M0' } } } } });
  if (u.pathname.endsWith('GetMeterRealtimeData.cgi')) return json(res, { Body: { Data: { '0': { Details: { Model: 'Smart Meter 63A-3', Manufacturer: 'Fronius', Serial: 'M0' }, PowerReal_P_Sum: 123.4, Frequency_Phase_Average: 50.0 } } } });
  if (u.pathname.endsWith('GetInverterRealtimeData.cgi')) return json(res, { Body: { Data: { PAC: { Value: 4200, Unit: 'W' }, FAC: { Value: 50, Unit: 'Hz' }, UAC: { Value: 240, Unit: 'V' }, IAC: { Value: 17.5, Unit: 'A' }, UDC: { Value: 600, Unit: 'V' }, IDC: { Value: 7, Unit: 'A' }, TOTAL_ENERGY: { Value: 1000, Unit: 'Wh' } } } });
  if (u.pathname.endsWith('GetPowerFlowRealtimeData.fcgi')) return json(res, { Body: { Data: { Site: { P_PV: 12600, P_Grid: -100, P_Load: -12500, P_Akku: null, rel_Autonomy: 100, rel_SelfConsumption: 99, E_Day: 50000, E_Year: 1e6, E_Total: 1e7 } } } });
  json(res, { APIVersion: 1 });
}).listen(port, '127.0.0.1', () => console.log('fake solar api on', port));
