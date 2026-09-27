/** Local HTTP fixture that exercises transport timeouts without contacting a provider. */
import { createServer } from "node:http";

/** Open a non-responding endpoint and return an explicit socket-cleanup boundary. */
export async function unresponsiveHttpServer() {
  const server = createServer((_request, _response) => { /* Intentionally never responds. */ });
  await new Promise<void>(resolve => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  if (!address || typeof address === "string") throw new Error("Missing HTTP test address");
  return { url: `http://127.0.0.1:${address.port}`, close: async () => {
    server.closeAllConnections();
    await new Promise<void>(resolve => server.close(() => resolve()));
  } };
}
