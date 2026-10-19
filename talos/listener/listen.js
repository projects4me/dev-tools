#!/usr/bin/env node
/**
 * Talos Hermes listener: one Socket.IO connection for one agent.
 *
 * Connects with the agent's Gaia OAuth token (env TALOS_TOKEN, never argv),
 * sends intents:set, then prints one JSON line per message on stdout:
 *   {"type":"ready","accepted":[...],"rejected":[...]}
 *   {"type":"event","envelope":{...}}
 *   {"type":"error","message":"..."}
 * Exits on disconnect or error; the Python supervisor restarts it with a
 * fresh token and reconciles anything missed while it was down.
 *
 * Usage: node listen.js --hermes-url <url> --intents '<json array>'
 */
'use strict';

const { io: createClient } = require('socket.io-client');

function parseArgs(argv) {
	const options = { hermesUrl: null, intents: [] };
	for (let i = 2; i < argv.length; i++) {
		if (argv[i] === '--hermes-url' && argv[i + 1]) {
			options.hermesUrl = argv[++i].replace(/\/$/, '');
		} else if (argv[i] === '--intents' && argv[i + 1]) {
			options.intents = JSON.parse(argv[++i]);
		}
	}
	return options;
}

function emit(message) {
	process.stdout.write(JSON.stringify(message) + '\n');
}

function fail(message) {
	emit({ type: 'error', message });
	process.exit(1);
}

async function main() {
	const options = parseArgs(process.argv);
	const token = process.env.TALOS_TOKEN;
	if (!options.hermesUrl) {
		fail('missing --hermes-url');
	}
	if (!token) {
		fail('missing TALOS_TOKEN');
	}
	if (!Array.isArray(options.intents) || options.intents.length === 0) {
		fail('missing --intents (non-empty array)');
	}

	const client = createClient(options.hermesUrl, {
		auth: { token },
		transports: ['websocket'],
		reconnection: false
	});

	client.on('connect_error', (error) => fail(`connect failed: ${error.message}`));
	client.on('disconnect', (reason) => fail(`disconnected: ${reason}`));
	client.on('domain:event', (envelope) => emit({ type: 'event', envelope }));

	client.on('connect', async () => {
		let ack;
		try {
			ack = await client.timeout(10000).emitWithAck('intents:set', {
				protocolVersion: 1,
				revision: 1,
				intents: options.intents
			});
		} catch (error) {
			fail(`intents:set failed: ${error.message}`);
		}
		if (!ack || !Array.isArray(ack.accepted) || ack.accepted.length === 0) {
			fail(`intents:set rejected: ${JSON.stringify(ack || null)}`);
		}
		emit({ type: 'ready', accepted: ack.accepted, rejected: ack.rejected || [] });
	});
}

main().catch((error) => fail(error.message));
