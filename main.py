import network
import time
import json
from machine import Pin, ADC
import urequests
import ntptime
from config import WIFI_SSID, WIFI_PASSWORD, BOT_TOKEN

TEXTO_AYUDA = (
    "Comandos disponibles:\n"
    "/humedad - Última humedad medida\n"
    "/historial - Últimas lecturas guardadas\n"
    "/umbral - Umbral de humedad configurado\n"
    "/estado - WiFi, última lectura y tiempo encendido\n"
    "/pausar - Pausar las alertas automáticas\n"
    "/reanudar - Reactivar las alertas automáticas\n"
    "/ayuda - Ver esta lista"
)

# ==========================================
# ENUM: EstadoHumedad
# ==========================================
class EstadoHumedad:
    ALTA = "ALTA"
    MEDIA = "MEDIA"
    BAJA = "BAJA"
    SIN_DATO = "SIN_DATO"

# ==========================================
# CLASE: SensorHumedad (UC1, UC12)
# ==========================================
class SensorHumedad:
    def __init__(self, pin=34, seco=4095, humedo=0, minimo_valido=1, maximo_valido=4094):
        self.pin = pin
        self.valorHumedad = 0
        self.valorSeco = seco
        self.valorHumedo = humedo
        # Los extremos del ADC (0 y 4095) se toman como valores imposibles:
        # indican sensor desconectado o en cortocircuito (UC12).
        self.minimoValido = minimo_valido
        self.maximoValido = maximo_valido
        self.adc = ADC(Pin(pin))
        self.adc.atten(ADC.ATTN_11DB)

    def medirHumedad(self):
        valor_crudo = self.adc.read()
        if not self.esLecturaValida(valor_crudo):
            return None
        porcentaje = int((self.valorSeco - valor_crudo) / (self.valorSeco - self.valorHumedo) * 100)
        self.valorHumedad = max(0, min(100, porcentaje))
        return self.valorHumedad

    def calibrarSensor(self, seco, humedo):
        self.valorSeco = seco
        self.valorHumedo = humedo

    def esLecturaValida(self, valor):
        return self.minimoValido <= valor <= self.maximoValido

# ==========================================
# CLASE: MÓDULO RTC DS1302 (3 Cables)
# ==========================================
class RelojDS1302:
    def __init__(self, clk_pin=18, dat_pin=19, rst_pin=21):
        self.clk = Pin(clk_pin, Pin.OUT)
        self.dat = Pin(dat_pin)
        self.rst = Pin(rst_pin, Pin.OUT)
        self.rst.value(0)
        self.clk.value(0)
        self._writereg(0x8E, 0)

    def _dec2bcd(self, dat):
        return (dat // 10) * 16 + (dat % 10)

    def _bcd2dec(self, dat):
        return (dat // 16) * 10 + (dat % 16)

    def _write_byte(self, dat):
        self.dat.init(Pin.OUT)
        for i in range(8):
            self.dat.value((dat >> i) & 1)
            self.clk.value(1)
            self.clk.value(0)

    def _read_byte(self):
        self.dat.init(Pin.IN)
        res = 0
        for i in range(8):
            res = res | (self.dat.value() << i)
            self.clk.value(1)
            self.clk.value(0)
        return res

    def _writereg(self, reg, dat):
        self.rst.value(1)
        self._write_byte(reg)
        self._write_byte(dat)
        self.rst.value(0)

    def sincronizar_con_internet(self):
        try:
            print("Sincronizando hora por internet (NTP)...")
            ntptime.host = "pool.ntp.org"
            ntptime.settime()
            hora_argentina_segundos = time.time() - 10800
            tm = time.localtime(hora_argentina_segundos)
            anio, mes, dia = tm[0], tm[1], tm[2]
            hora, minuto, segundo = tm[3], tm[4], tm[5]

            self._writereg(0x8E, 0)
            self.rst.value(1)
            self._write_byte(0xBE)
            self._write_byte(self._dec2bcd(segundo))
            self._write_byte(self._dec2bcd(minuto))
            self._write_byte(self._dec2bcd(hora))
            self._write_byte(self._dec2bcd(dia))
            self._write_byte(self._dec2bcd(mes))
            self._write_byte(1)
            self._write_byte(self._dec2bcd(anio - 2000))
            # En modo ráfaga el DS1302 solo guarda los datos si se escriben los 8 registros
            self._write_byte(0)
            self.rst.value(0)
            print(f"¡DS1302 sincronizado automáticamente: {anio}/{mes:02d}/{dia:02d} {hora:02d}:{minuto:02d}:{segundo:02d}!")
        except Exception as e:
            print("No se pudo sincronizar con NTP:", e)

    def obtener_fecha_hora(self):
        self.rst.value(1)
        self._write_byte(0xBF)
        data = [self._read_byte() for _ in range(7)]
        self.rst.value(0)
        seg = self._bcd2dec(data[0] & 0x7F)
        minu = self._bcd2dec(data[1])
        hora = self._bcd2dec(data[2])
        dia = self._bcd2dec(data[3])
        mes = self._bcd2dec(data[4])
        an = self._bcd2dec(data[6]) + 2000
        return an, mes, dia, hora, minu, seg

    def obtener_tiempo(self):
        try:
            an, mes, dia, hora, minu, seg = self.obtener_fecha_hora()
            return f"{an}-{mes:02d}-{dia:02d} {hora:02d}:{minu:02d}:{seg:02d}"
        except Exception:
            return "2026-00-00 00:00:00"

# ==========================================
# CLASES DE NOTIFICACIÓN (Jerarquía UML)
# ==========================================
class Notificacion:
    def __init__(self, fecha, mensaje):
        self.fecha = fecha
        self.mensaje = mensaje

    def mostrarPorcentaje(self):
        return self.mensaje

    def enviar(self):
        return True

class AlertaHumedadBaja(Notificacion):
    def __init__(self, fecha, porcentaje, umbral, recordatorio=False):
        if recordatorio:
            titulo = "⏰ *RECORDATORIO: la planta sigue sedienta* ⏰"
        else:
            titulo = "⚠️ *¡ALERTA SEDIENTA!* ⚠️"
        mensaje = f"{titulo}\nHora: {fecha}\nHumedad: {porcentaje}% (Umbral: {umbral}%)"
        super().__init__(fecha, mensaje)
        self.porcentaje = porcentaje
        self.umbral = umbral

    def generarAlerta(self):
        print(self.mensaje)

class RespuestaEstado(Notificacion):
    def __init__(self, fecha, comando, contenido):
        super().__init__(fecha, contenido)
        self.comando = comando

    def responderComando(self):
        return self.mensaje

# ==========================================
# CLASE: ESP32 (UC4, UC5)
# ==========================================
class ESP32:
    def __init__(self, modelo="ESP32 NodeMCU", pines=[18, 19, 21, 34]):
        self.modelo = modelo
        self.pinesDisponibles = pines
        self.conexionWifi = False
        self.wlan = None
        self.ssid = ""
        self.password = ""
        self.sensor = SensorHumedad(34)
        self.rtc = RelojDS1302()

    def conectarWifi(self, ssid, password):
        self.ssid = ssid
        self.password = password
        self.wlan = network.WLAN(network.STA_IF)
        self.wlan.active(True)
        print(f"Conectando a Wi-Fi: {ssid}")
        while not self._intentarConexion():
            print("\nNo se pudo conectar, reintentando en 5 segundos...")
            time.sleep(5)
        self.conexionWifi = True
        print(f"\n¡Conectado a la red con éxito! IP: {self.obtenerIP()}")
        return self.conexionWifi

    def _intentarConexion(self, espera_s=15):
        if self.wlan.isconnected():
            return True
        try:
            self.wlan.disconnect()
        except Exception:
            pass
        self.wlan.connect(self.ssid, self.password)
        inicio = time.ticks_ms()
        while not self.wlan.isconnected():
            if time.ticks_diff(time.ticks_ms(), inicio) > espera_s * 1000:
                return False
            time.sleep(0.5)
            print(".", end="")
        return True

    def verificarConexion(self):
        # Devuelve True si hubo que reconectar
        if self.wlan.isconnected():
            self.conexionWifi = True
            return False
        self.conexionWifi = False
        print("\nSe perdió la conexión Wi-Fi. Reintentando...")
        time.sleep(3)
        while not self._intentarConexion():
            print("\nReintento fallido, nuevo intento en 5 segundos...")
            time.sleep(5)
        self.conexionWifi = True
        print(f"\n¡Reconectado a Wi-Fi! IP: {self.obtenerIP()}")
        return True

    def obtenerIP(self):
        if self.wlan and self.wlan.isconnected():
            return self.wlan.ifconfig()[0]
        return "-"

    def obtenerSenal(self):
        try:
            return self.wlan.status("rssi")
        except Exception:
            return None

    def leerSensor(self):
        return self.sensor.medirHumedad()

    def enviarDatos(self, notificacion):
        return notificacion.enviar()

# ==========================================
# CLASE: BOT DE TELEGRAM (UC6, UC7)
# ==========================================
class BotTelegram:
    def __init__(self, token, archivo_chat="chat_vinculado.txt"):
        self.token = token
        self.archivo_chat = archivo_chat
        self.chat_id = self._cargar_chat()
        self.offset_id = 0

    def _cargar_chat(self):
        try:
            with open(self.archivo_chat, "r") as f:
                return f.read().strip() or None
        except OSError:
            return None

    def vincular_chat(self, chat_id):
        self.chat_id = str(chat_id)
        try:
            with open(self.archivo_chat, "w") as f:
                f.write(self.chat_id)
        except OSError as e:
            print("No se pudo guardar el chat vinculado:", e)

    def es_autorizado(self, chat_id):
        return self.chat_id is not None and str(chat_id) == self.chat_id

    def enviar_mensaje(self, texto, chat_id=None):
        destino = chat_id if chat_id is not None else self.chat_id
        if destino is None:
            print(f"[Telegram] Sin chat vinculado, no se envió: {texto}")
            return False
        try:
            url = f"https://api.telegram.org/bot{self.token}/sendMessage"
            cuerpo = json.dumps({"chat_id": destino, "text": texto}).encode("utf-8")
            respuesta = urequests.post(url, data=cuerpo, headers={"Content-Type": "application/json"})
            enviado = respuesta.status_code == 200
            respuesta.close()
            return enviado
        except Exception as e:
            print(f"Error Telegram: {e}")
            return False

    def obtener_actualizaciones(self):
        try:
            url = f"https://api.telegram.org/bot{self.token}/getUpdates?offset={self.offset_id}&timeout=1"
            respuesta = urequests.get(url)
            if respuesta.status_code == 200:
                datos = respuesta.json()
                respuesta.close()
                return datos.get("result", [])
            respuesta.close()
        except Exception:
            pass
        return []

# ==========================================
# CLASE PRINCIPAL: MacetaInteligente
# ==========================================
class MacetaInteligente:
    def __init__(self, nombre, ssid, password, bot_token):
        self.nombre = nombre
        # Valores fijos del software: el usuario los consulta pero no los modifica desde el bot
        self.umbralHumedad = 30
        self.histeresis = 3  # % por encima del umbral para dar la humedad por recuperada (UC10)
        self.intervaloRecordatorioMs = 6 * 60 * 60 * 1000  # 6 horas (UC11)
        self.lecturasParaFalla = 5  # lecturas inválidas seguidas para avisar falla (UC12)
        self.cantidadHistorial = 10  # UC16
        self.horaResumen = 20  # 20:00 hs (UC17)

        self.ultimoPorcentaje = None
        self.fechaUltimaLectura = ""
        self.estadoActual = EstadoHumedad.SIN_DATO

        self.esp32 = ESP32()
        self.bot = BotTelegram(bot_token)
        self.ssid = ssid
        self.password = password

        self.notificacionesPausadas = False
        self.alertaActiva = False
        self.ultimaAlertaMs = 0
        self.lecturasInvalidas = 0
        self.fallaSensor = False
        self.historial = []
        self.sumaDia = 0
        self.cantidadDia = 0
        self.minimoDia = None
        self.maximoDia = None
        self.diaUltimoResumen = None
        self.inicioSegundos = time.time()

    def configurarUmbral(self, valor):
        self.umbralHumedad = valor
        return True

    def notificar(self, notificacion):
        return self.bot.enviar_mensaje(notificacion.mostrarPorcentaje())

    # UC1
    def verificarHumedad(self):
        porcentaje = self.esp32.leerSensor()
        if porcentaje is None:
            self.lecturasInvalidas += 1
            self.estadoActual = EstadoHumedad.SIN_DATO
            return self.estadoActual

        self.lecturasInvalidas = 0
        self.ultimoPorcentaje = porcentaje
        self.fechaUltimaLectura = self.esp32.rtc.obtener_tiempo()

        if porcentaje > 60:
            self.estadoActual = EstadoHumedad.ALTA
        elif porcentaje >= self.umbralHumedad:
            self.estadoActual = EstadoHumedad.MEDIA
        else:
            self.estadoActual = EstadoHumedad.BAJA
        return self.estadoActual

    # UC12
    def revisarFallaSensor(self):
        if not self.fallaSensor and self.lecturasInvalidas >= self.lecturasParaFalla:
            self.fallaSensor = True
            timestamp = self.esp32.rtc.obtener_tiempo()
            print("Falla del sensor detectada")
            self.notificar(Notificacion(timestamp, f"🛠️ Revisar el sensor.\nHora: {timestamp}\nLa lectura está fuera de rango: puede estar desconectado o dañado. Las alertas de humedad quedan suspendidas mientras dure la falla."))
        elif self.fallaSensor and self.lecturasInvalidas == 0:
            self.fallaSensor = False
            timestamp = self.esp32.rtc.obtener_tiempo()
            self.notificar(Notificacion(timestamp, f"✅ El sensor volvió a funcionar.\nHora: {timestamp}\nHumedad actual: {self.ultimoPorcentaje}%"))

    # UC2, UC10, UC11
    def revisarAlertas(self, tiempo_actual):
        if self.bot.chat_id is None or self.estadoActual == EstadoHumedad.SIN_DATO:
            return
        porcentaje = self.ultimoPorcentaje
        timestamp = self.fechaUltimaLectura

        if porcentaje < self.umbralHumedad:
            if self.notificacionesPausadas:
                return
            if not self.alertaActiva:
                alerta = AlertaHumedadBaja(timestamp, porcentaje, self.umbralHumedad)
                alerta.generarAlerta()
                self.notificar(alerta)
                self.alertaActiva = True
                self.ultimaAlertaMs = tiempo_actual
            elif time.ticks_diff(tiempo_actual, self.ultimaAlertaMs) >= self.intervaloRecordatorioMs:
                recordatorio = AlertaHumedadBaja(timestamp, porcentaje, self.umbralHumedad, recordatorio=True)
                recordatorio.generarAlerta()
                self.notificar(recordatorio)
                self.ultimaAlertaMs = tiempo_actual
        elif self.alertaActiva and porcentaje >= self.umbralHumedad + self.histeresis:
            if not self.notificacionesPausadas:
                self.notificar(Notificacion(timestamp, f"💧 La planta ya tiene agua.\nHora: {timestamp}\nHumedad: {porcentaje}%"))
            self.alertaActiva = False

    # UC17
    def revisarResumenDiario(self):
        if self.bot.chat_id is None:
            return
        try:
            an, mes, dia, hora, minu, seg = self.esp32.rtc.obtener_fecha_hora()
        except Exception:
            return
        hoy = (an, mes, dia)
        if hora != self.horaResumen or self.diaUltimoResumen == hoy:
            return
        self.diaUltimoResumen = hoy
        if self.cantidadDia == 0:
            return

        promedio = self.sumaDia / self.cantidadDia
        timestamp = f"{an}-{mes:02d}-{dia:02d} {hora:02d}:{minu:02d}:{seg:02d}"
        self.notificar(Notificacion(timestamp, (
            f"📊 Resumen diario ({dia:02d}/{mes:02d}/{an})\n"
            f"Humedad promedio: {promedio:.0f}%\n"
            f"Mínima: {self.minimoDia}%\n"
            f"Máxima: {self.maximoDia}%\n"
            f"Lecturas: {self.cantidadDia}"
        )))
        self.sumaDia = 0
        self.cantidadDia = 0
        self.minimoDia = None
        self.maximoDia = None

    def tiempoEncendido(self):
        segundos = int(time.time() - self.inicioSegundos)
        dias, segundos = divmod(segundos, 86400)
        horas, segundos = divmod(segundos, 3600)
        minutos = segundos // 60
        return f"{dias} d {horas:02d} h {minutos:02d} min"

    def procesarComando(self, comando, chat_id):
        timestamp = self.esp32.rtc.obtener_tiempo()
        comando = comando.split()[0].split("@")[0].lower()

        # UC6
        if comando == "/start":
            if self.bot.chat_id is None:
                self.bot.vincular_chat(chat_id)
                return RespuestaEstado(timestamp, comando, f"👋 ¡Hola! Quedaste vinculado a la maceta '{self.nombre}'. Acá vas a recibir las notificaciones.\n\n{TEXTO_AYUDA}")
            if self.bot.es_autorizado(chat_id):
                return RespuestaEstado(timestamp, comando, f"Ya estás vinculado a la maceta '{self.nombre}'.\n\n{TEXTO_AYUDA}")

        if self.bot.chat_id is None:
            return RespuestaEstado(timestamp, comando, "Enviá /start para vincularte con la maceta.")

        # UC7
        if not self.bot.es_autorizado(chat_id):
            print(f"Chat no autorizado: {chat_id}")
            return RespuestaEstado(timestamp, comando, "⛔ No autorizado.")

        # UC8
        if comando == "/ayuda":
            return RespuestaEstado(timestamp, comando, TEXTO_AYUDA)

        # UC3
        if comando == "/humedad":
            if self.ultimoPorcentaje is None:
                return RespuestaEstado(timestamp, comando, "Todavía no hay lecturas disponibles.")
            texto = f"💧 Humedad actual: {self.ultimoPorcentaje}%\nLectura: {self.fechaUltimaLectura}"
            if self.fallaSensor:
                texto += "\n⚠️ El sensor tiene una falla: el valor puede no estar actualizado."
            return RespuestaEstado(timestamp, comando, texto)

        # UC13
        if comando == "/umbral":
            return RespuestaEstado(timestamp, comando, f"🎯 Umbral de humedad: {self.umbralHumedad}%\n(Está definido en el software y no se puede modificar desde el bot.)")

        # UC14
        if comando == "/pausar":
            if self.notificacionesPausadas:
                return RespuestaEstado(timestamp, comando, "Las notificaciones ya estaban pausadas.")
            self.notificacionesPausadas = True
            return RespuestaEstado(timestamp, comando, "🔕 Notificaciones pausadas")

        # UC15
        if comando == "/reanudar":
            if not self.notificacionesPausadas:
                return RespuestaEstado(timestamp, comando, "Las notificaciones ya estaban activadas.")
            self.notificacionesPausadas = False
            return RespuestaEstado(timestamp, comando, "🔔 Notificaciones activadas")

        # UC16
        if comando == "/historial":
            historial_texto = self.leer_historial()
            return RespuestaEstado(timestamp, comando, f"📋 *Historial*:\n{historial_texto}")

        # UC18
        if comando == "/estado":
            if self.esp32.wlan and self.esp32.wlan.isconnected():
                senal = self.esp32.obtenerSenal()
                wifi = f"conectado (IP {self.esp32.obtenerIP()}" + (f", señal {senal} dBm)" if senal is not None else ")")
            else:
                wifi = "desconectado"
            if self.ultimoPorcentaje is None:
                lectura = "sin lecturas"
            else:
                lectura = f"{self.ultimoPorcentaje}% ({self.fechaUltimaLectura})"
            return RespuestaEstado(timestamp, comando, (
                "📡 Estado del sistema\n"
                f"WiFi: {wifi}\n"
                f"Última lectura: {lectura}\n"
                f"Sensor: {'con falla, revisar' if self.fallaSensor else 'OK'}\n"
                f"Notificaciones: {'pausadas' if self.notificacionesPausadas else 'activas'}\n"
                f"Tiempo encendido: {self.tiempoEncendido()}"
            ))

        return RespuestaEstado(timestamp, comando, f"Comando no reconocido: {comando}\nEnviá /ayuda para ver los comandos.")

    def atenderComandos(self):
        for resultado in self.bot.obtener_actualizaciones():
            self.bot.offset_id = resultado["update_id"] + 1
            mensaje = resultado.get("message", {})
            texto = mensaje.get("text", "").strip()
            chat_id = mensaje.get("chat", {}).get("id")
            if not texto or chat_id is None:
                continue
            respuesta = self.procesarComando(texto, chat_id)
            self.bot.enviar_mensaje(respuesta.responderComando(), chat_id)

    # Las últimas lecturas se guardan en memoria (UC16) junto con los datos del resumen diario (UC17)
    def guardar_registro(self, timestamp, humedad):
        self.historial.append((timestamp, humedad))
        if len(self.historial) > self.cantidadHistorial:
            self.historial.pop(0)
        self.sumaDia += humedad
        self.cantidadDia += 1
        if self.minimoDia is None or humedad < self.minimoDia:
            self.minimoDia = humedad
        if self.maximoDia is None or humedad > self.maximoDia:
            self.maximoDia = humedad

    def leer_historial(self):
        if not self.historial:
            return "No hay registros."
        return "\n".join(f"[{fecha}] Humedad: {humedad}%" for fecha, humedad in self.historial)

    def iniciarMonitoreo(self):
        self.esp32.conectarWifi(self.ssid, self.password)
        self.esp32.rtc.sincronizar_con_internet()
        self.inicioSegundos = time.time()
        print(f"Sistema '{self.nombre}' iniciado. Guardando registros cada 10 segundos exactos...")

        # UC9
        timestamp = self.esp32.rtc.obtener_tiempo()
        estado = self.verificarHumedad()
        if estado == EstadoHumedad.SIN_DATO:
            humedad_texto = "sin dato (revisar el sensor)"
        else:
            humedad_texto = f"{self.ultimoPorcentaje}%"
            self.guardar_registro(self.fechaUltimaLectura, self.ultimoPorcentaje)

        if self.bot.chat_id is None:
            print("No hay chat vinculado: enviá /start al bot para recibir las notificaciones.")
        else:
            notif_inicial = Notificacion(timestamp, f"🌱 Maceta conectada: '{self.nombre}'\nHora RTC: {timestamp}\nHumedad actual: {humedad_texto}")
            self.notificar(notif_inicial)

        ultimo_chequeo_telegram = time.ticks_ms()
        ultima_lectura_ms = time.ticks_ms()
        ultimo_registro_ms = time.ticks_ms()
        intervalo_lectura_ms = 2000
        intervalo_registro_ms = 10000

        try:
            while True:
                # UC5: si se reconectó, no se recuperan los registros del tiempo sin conexión
                if self.esp32.verificarConexion():
                    ultimo_registro_ms = time.ticks_ms()

                tiempo_actual = time.ticks_ms()

                if time.ticks_diff(tiempo_actual, ultima_lectura_ms) >= intervalo_lectura_ms:
                    ultima_lectura_ms = tiempo_actual
                    self.verificarHumedad()
                    self.revisarFallaSensor()
                    self.revisarAlertas(tiempo_actual)
                    self.revisarResumenDiario()

                if time.ticks_diff(tiempo_actual, ultimo_registro_ms) >= intervalo_registro_ms:
                    ultimo_registro_ms += intervalo_registro_ms
                    if self.estadoActual != EstadoHumedad.SIN_DATO:
                        self.guardar_registro(self.fechaUltimaLectura, self.ultimoPorcentaje)
                        print(f"Registro: [{self.fechaUltimaLectura}] {self.ultimoPorcentaje}% (Estado: {self.estadoActual})")

                if time.ticks_diff(tiempo_actual, ultimo_chequeo_telegram) > 3000:
                    ultimo_chequeo_telegram = time.ticks_ms()
                    self.atenderComandos()

                time.sleep_ms(200)

        except KeyboardInterrupt:
            print("\nDetenido.")

# ==========================================
# EJECUCIÓN
# ==========================================
maceta = MacetaInteligente(
    nombre="Mi Maceta Principal",
    ssid=WIFI_SSID,
    password=WIFI_PASSWORD,
    bot_token=BOT_TOKEN
)

maceta.iniciarMonitoreo()
