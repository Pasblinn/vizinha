const { default: makeWASocket, useMultiFileAuthState, DisconnectReason, fetchLatestBaileysVersion } = require('@whiskeysockets/baileys');
const { Boom } = require('@hapi/boom');
const qrcode = require('qrcode-terminal');
const cron = require('node-cron');
const pino = require('pino');

// ============================================================
// CONFIGURAÇÃO
// ============================================================
const NUMERO_DESTINO = '554299860111';
const MENSAGEM = 'Por favor boa noite, estou tentando dormir porém o ap debaixo aqui está muito barulhento poderia verificar ?';
// ============================================================

let sock = null;
let conectado = false;
let agendamentoAtivo = false;

// Espera um tempo aleatório entre min e max milissegundos
function esperar(minMs, maxMs) {
  const ms = Math.floor(Math.random() * (maxMs - minMs + 1)) + minMs;
  return new Promise(resolve => setTimeout(resolve, ms));
}

async function enviarMensagemHumanizada() {
  if (!conectado || !sock) {
    console.log('❌ Não conectado na hora do envio.');
    return;
  }

  const jid = `${NUMERO_DESTINO}@s.whatsapp.net`;

  try {
    // 1. Aparece como "online" antes de digitar
    await sock.sendPresenceUpdate('available', jid);
    await esperar(1500, 3500);

    // 2. Simula "digitando..." pelo tempo proporcional ao tamanho da mensagem
    await sock.sendPresenceUpdate('composing', jid);
    const tempoDigitando = MENSAGEM.length * 60 + Math.floor(Math.random() * 2000); // ~60ms por caractere + variação
    await esperar(tempoDigitando, tempoDigitando + 1500);

    // 3. Para de "digitar" por um segundo (pausa natural)
    await sock.sendPresenceUpdate('paused', jid);
    await esperar(500, 1500);

    // 4. Envia a mensagem
    await sock.sendMessage(jid, { text: MENSAGEM });

    // 5. Volta a "offline/available" após enviar
    await esperar(1000, 2500);
    await sock.sendPresenceUpdate('unavailable', jid);

    console.log(`✅ [${new Date().toLocaleTimeString('pt-BR')}] Mensagem enviada para ${NUMERO_DESTINO}`);
  } catch (err) {
    console.error('❌ Erro ao enviar:', err.message);
  }
}

function agendarMensagem() {
  if (agendamentoAtivo) return;
  agendamentoAtivo = true;

  // Cron às 21:27 com delay aleatório de 0–16 min (cobre até 21:43)
  cron.schedule('27 21 * * *', async () => {
    const delayMin = Math.floor(Math.random() * 17);
    const horarioEnvio = new Date();
    horarioEnvio.setMinutes(horarioEnvio.getMinutes() + delayMin);
    console.log(`⏳ Mensagem programada para ${horarioEnvio.toLocaleTimeString('pt-BR')} (${delayMin} min de delay)`);

    setTimeout(enviarMensagemHumanizada, delayMin * 60 * 1000);
  }, { timezone: 'America/Sao_Paulo' });

  console.log('📅 Agendamento ativo — mensagem diária entre 21:27 e 21:43 (Brasília)');
}

async function conectar() {
  const { state, saveCreds } = await useMultiFileAuthState('auth_session');
  const { version } = await fetchLatestBaileysVersion();

  sock = makeWASocket({
    version,
    auth: state,
    printQRInTerminal: false,
    logger: pino({ level: 'silent' }),
  });

  sock.ev.on('connection.update', ({ connection, lastDisconnect, qr }) => {
    if (qr) {
      console.log('\n📱 Escaneie o QR Code com seu WhatsApp:\n');
      qrcode.generate(qr, { small: true });
    }

    if (connection === 'open') {
      conectado = true;
      console.log('✅ WhatsApp conectado! Aguardando horário agendado (21:27–21:43)...');
      agendarMensagem();
    }

    if (connection === 'close') {
      conectado = false;
      const codigo = new Boom(lastDisconnect?.error)?.output?.statusCode;
      const deveReconectar = codigo !== DisconnectReason.loggedOut;

      if (deveReconectar) {
        console.log('🔄 Reconectando em 10s...');
        setTimeout(conectar, 10000);
      } else {
        console.log('🚪 Sessão encerrada. Apague auth_session/ e reinicie.');
        process.exit(1);
      }
    }
  });

  sock.ev.on('creds.update', saveCreds);
}

conectar();
