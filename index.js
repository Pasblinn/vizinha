const { default: makeWASocket, useMultiFileAuthState, DisconnectReason, fetchLatestBaileysVersion } = require('@whiskeysockets/baileys');
const { Boom } = require('@hapi/boom');
const qrcode = require('qrcode-terminal');
const cron = require('node-cron');
const pino = require('pino');

// ============================================================
// CONFIGURAÇÃO
// ============================================================
const NUMERO_DESTINO = '554299860111'; // DDI + DDD + número sem + espaços ou traços
const MENSAGEM = 'Por favor boa noite, estou tentando dormir porém o ap debaixo aqui está muito barulhento poderia verificar ?';
// ============================================================

let sock = null;
let conectado = false;

async function conectar() {
  const { state, saveCreds } = await useMultiFileAuthState('auth_session');
  const { version } = await fetchLatestBaileysVersion();

  sock = makeWASocket({
    version,
    auth: state,
    printQRInTerminal: false,
    logger: pino({ level: 'silent' }), // sem logs desnecessários
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
        console.log('🔄 Reconectando...');
        setTimeout(conectar, 5000);
      } else {
        console.log('🚪 Sessão encerrada. Apague a pasta auth_session e reinicie.');
        process.exit(1);
      }
    }
  });

  sock.ev.on('creds.update', saveCreds);
}

let agendamentoAtivo = false;

function agendarMensagem() {
  if (agendamentoAtivo) return;
  agendamentoAtivo = true;

  // Dispara às 21:27, depois escolhe delay aleatório de 0–16 min (cobre até 21:43)
  cron.schedule('27 21 * * *', async () => {
    const delayMin = Math.floor(Math.random() * 17);
    console.log(`⏳ Mensagem programada para daqui ${delayMin} minuto(s)...`);

    setTimeout(async () => {
      if (!conectado) {
        console.log('❌ Não estava conectado na hora do envio.');
        return;
      }
      try {
        const jid = `${NUMERO_DESTINO}@s.whatsapp.net`;
        await sock.sendMessage(jid, { text: MENSAGEM });
        console.log(`✅ [${new Date().toLocaleTimeString('pt-BR')}] Mensagem enviada para ${NUMERO_DESTINO}`);
      } catch (err) {
        console.error('❌ Erro ao enviar:', err.message);
      }
    }, delayMin * 60 * 1000);
  }, { timezone: 'America/Sao_Paulo' });

  console.log('📅 Agendamento ativo — mensagem diária entre 21:27 e 21:43 (horário de Brasília)');
}

conectar();
