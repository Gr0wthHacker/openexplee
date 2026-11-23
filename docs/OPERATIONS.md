# Operating OpenExplee

## Deployment

Use one application process per SQLite database. Keep the default loopback bind, or put a TLS reverse proxy in front of it and preserve the public Host header. Limit dashboard access to a private network, VPN, or authenticated proxy. Set a request body limit at the proxy, allow enough time for website analysis, and rate limit login failures. For Worker replies, expose only `POST /api/cloudflare/inbound-webhook` to the public internet and configure its independent bearer token.

The application uses one operator identity. Deploy separate instances and volumes wherever business data or operator access must be isolated. Use a volume with disk encryption and tightly restrict its backups. SQLite holds inbox passwords, persisted provider keys, and conversation contents. The process uses a non-root UID and a private database file.

Configure the postal address, opt-out handling, and an inbox that receives replies before enabling sending for a business. Add suppression entries through the leads API. Automatic follow-ups default to off and need a configured IMAP account because the scheduler uses it to check for replies before sending. Automatic model-written replies default to off. Enable them explicitly with `AUTO_REPLY_ENABLED=true` after validating your inbound setup and preferred model provider. Prior dashboard settings cannot enable them after this upgrade.

Choose the LLM provider explicitly. Cloud providers receive prospect and product context when drafting. Local Ollama keeps that processing on the configured Ollama server. Use a trusted endpoint. Web search sends the generated research query to the configured search service.

## Upgrade from the upstream version

1. Stop the upstream application. Make a consistent backup of the SQLite volume, including any WAL files that remain after shutdown. Keep the backup private.
2. Inspect forwarding rules, inbox CC and Reply-To values, stored drafts, booking links, and the configured LLM provider. Old drafts and inbox records can contain upstream-specific destinations. Review old queued drafts before launching.
3. Existing Docker volumes created by the old root-running image may need an ownership migration. With the application stopped and the correct Compose project selected, run:

   ```sh
   docker compose run --rm --no-deps --user 0:0 --cap-add CHOWN --entrypoint chown openexplee -R 10001:10001 /app/data
   ```

4. Configure the operator password and start with `docker compose up --build -d`. If a Worker was installed, update its deployment and independently configure the callback token and forwarding address.
5. Test login, a draft campaign, reply ingestion, and one delivery to a mailbox you control before opening normal campaign traffic.

If the Compose CLI in use does not support `run --cap-add`, use an isolated Alpine container with the verified application volume mounted and only `CHOWN` added.

## Backup and restore

Take periodic SQLite backups with the SQLite backup API or stop the service and copy the volume consistently. Test restoring to a separate volume on a separate instance. Protect backup copies like source credentials. For rollback, stop the new container and restore the original image and a tested pre-upgrade backup together.

## Interrupted deliveries

A provider accepted send and a SQLite commit cannot be made atomic. If a send is interrupted or its outcome is ambiguous, OpenExplee moves the lead to `send_review` and excludes it from automatic dispatch. Check the provider's sent log before reapproving any lead. Daily quota counts outbound attempts, including an ambiguous attempt. Worker forwarding and webhook acceptance are separate events. Check both provider and application logs if inbound mail is missing.

## Verification

```sh
python -m pytest -q
(cd cloudflare-worker && npm ci && npm run typecheck && npm audit)
OPENEXPLEE_ADMIN_PASSWORD=compose-validation-only docker compose config --quiet
```
