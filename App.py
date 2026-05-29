"""
Nebula Hub - Backend API para Render
Web-Based Script Hub - Servidor de Producción
"""

import os
import sys
import time
import threading
import logging
from dataclasses import dataclass
from typing import Dict, Optional

from flask import Flask, request, Response, jsonify
from flask_cors import CORS

# ============================================================
# CONFIGURACIÓN DE LOGGING PARA PRODUCCIÓN
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# ============================================================
# INICIALIZACIÓN DE LA APLICACIÓN FLASK
# ============================================================
app = Flask(__name__)

# Configuración de CORS - Permitir TODOS los orígenes (Netlify, localhost, etc.)
CORS(app, resources={
    r"/*": {
        "origins": "*",
        "methods": ["GET", "POST", "OPTIONS"],
        "allow_headers": ["Content-Type", "Authorization", "Accept"],
        "max_age": 3600,
        "supports_credentials": False
    }
})

# Tamaño máximo de payload: 1 MB (suficiente para código Luau)
app.config['MAX_CONTENT_LENGTH'] = 1 * 1024 * 1024

# Variable de tiempo de inicio
start_time = time.time()

# ============================================================
# MODELO DE DATOS (Almacenamiento en memoria)
# ============================================================
@dataclass
class ScriptEntry:
    """Estructura para almacenar un script pendiente"""
    codigo: str
    timestamp: float
    intentos: int = 0
    
    @property
    def tamanio(self) -> int:
        return len(self.codigo)
    
    @property
    def lineas(self) -> int:
        return self.codigo.count('\n') + 1

# Diccionario en memoria para almacenar scripts pendientes
# Clave: user_id (int), Valor: ScriptEntry
pending_scripts: Dict[int, ScriptEntry] = {}

# Mutex para operaciones thread-safe
scripts_lock = threading.Lock()

# Tiempo máximo de vida de un script pendiente (segundos)
SCRIPT_TIMEOUT = 300  # 5 minutos

# ============================================================
# LIMPIEZA AUTOMÁTICA DE SCRIPTS EXPIRADOS
# ============================================================
def limpiar_scripts_expirados():
    """Elimina scripts que han excedido su tiempo de vida"""
    while True:
        time.sleep(60)  # Revisar cada minuto
        
        ahora = time.time()
        ids_expirados = []
        
        with scripts_lock:
            for user_id, entry in pending_scripts.items():
                if ahora - entry.timestamp > SCRIPT_TIMEOUT:
                    ids_expirados.append(user_id)
            
            for user_id in ids_expirados:
                entry = pending_scripts[user_id]
                logger.info(
                    f"🧹 Script expirado eliminado | "
                    f"Usuario: {user_id} | "
                    f"Antigüedad: {int(ahora - entry.timestamp)}s"
                )
                del pending_scripts[user_id]

# Iniciar hilo de limpieza en background
limpieza_thread = threading.Thread(target=limpiar_scripts_expirados, daemon=True)
limpieza_thread.start()

# ============================================================
# RUTAS DE LA API
# ============================================================

@app.route('/health', methods=['GET'])
def health_check():
    """
    Endpoint de verificación de estado
    Usado por Netlify para verificar que el backend está vivo
    Usado por el frontend para mostrar estado de conexión
    """
    try:
        with scripts_lock:
            scripts_pendientes = len(pending_scripts)
        
        uptime = int(time.time() - start_time)
        
        return jsonify({
            "status": "ok",
            "timestamp": time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime()),
            "uptime_seconds": uptime,
            "uptime_formatted": f"{uptime // 3600}h {(uptime % 3600) // 60}m {uptime % 60}s",
            "pending_scripts": scripts_pendientes,
            "version": "2.0.0",
            "server": "Render"
        }), 200
        
    except Exception as e:
        logger.error(f"Error en health check: {str(e)}")
        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500


@app.route('/enviar-script', methods=['POST'])
def enviar_script():
    """
    Endpoint para recibir código Luau desde el frontend (Netlify)
    
    Espera JSON:
    {
        "usuario_id": 123456789,
        "codigo_script": "print('Hola Mundo')"
    }
    
    Retorna JSON con confirmación o error
    """
    try:
        # Verificar que sea una petición JSON
        if not request.is_json:
            logger.warning("📭 Petición sin Content-Type application/json")
            return jsonify({
                "error": "La petición debe ser JSON",
                "ayuda": "Usa Content-Type: application/json"
            }), 400
        
        # Obtener datos del body
        data = request.get_json(force=True, silent=False)
        
        if not data:
            logger.warning("📭 Body JSON vacío")
            return jsonify({
                "error": "No se recibieron datos JSON"
            }), 400
        
        # Extraer campos
        usuario_id = data.get('usuario_id')
        codigo_script = data.get('codigo_script')
        
        # ============================================================
        # VALIDACIONES
        # ============================================================
        
        # Validar que los campos existan
        if usuario_id is None or codigo_script is None:
            return jsonify({
                "error": "Campos requeridos faltantes",
                "campos_requeridos": ["usuario_id", "codigo_script"],
                "campos_recibidos": list(data.keys())
            }), 400
        
        # Validar usuario_id
        try:
            usuario_id = int(usuario_id)
        except (ValueError, TypeError):
            return jsonify({
                "error": "usuario_id debe ser un número entero",
                "valor_recibido": str(usuario_id)
            }), 400
        
        if usuario_id <= 0:
            return jsonify({
                "error": "usuario_id debe ser un número positivo",
                "valor_recibido": usuario_id
            }), 400
        
        if usuario_id > 9999999999:  # Límite razonable para IDs de Roblox
            return jsonify({
                "error": "usuario_id demasiado grande",
                "valor_recibido": usuario_id
            }), 400
        
        # Validar codigo_script
        if not isinstance(codigo_script, str):
            return jsonify({
                "error": "codigo_script debe ser un string",
                "tipo_recibido": type(codigo_script).__name__
            }), 400
        
        # Limpiar whitespace extremo pero preservar formato
        codigo_script = codigo_script.strip()
        
        if len(codigo_script) == 0:
            return jsonify({
                "error": "El código no puede estar vacío"
            }), 400
        
        # Límite de tamaño (50 KB)
        MAX_SIZE = 50000
        if len(codigo_script) > MAX_SIZE:
            return jsonify({
                "error": f"El código excede el límite de {MAX_SIZE} caracteres",
                "tamano_actual": len(codigo_script),
                "limite": MAX_SIZE
            }), 413
        
        # ============================================================
        # ALMACENAR SCRIPT
        # ============================================================
        
        with scripts_lock:
            # Verificar si ya existe un script para este usuario
            if usuario_id in pending_scripts:
                entrada_anterior = pending_scripts[usuario_id]
                logger.info(
                    f"🔄 Sobrescribiendo script existente | "
                    f"Usuario: {usuario_id} | "
                    f"Antigüedad anterior: {int(time.time() - entrada_anterior.timestamp)}s"
                )
            
            # Crear nueva entrada
            nueva_entrada = ScriptEntry(
                codigo=codigo_script,
                timestamp=time.time(),
                intentos=0
            )
            
            pending_scripts[usuario_id] = nueva_entrada
        
        # Log detallado
        logger.info(
            f"✅ Script almacenado | "
            f"Usuario: {usuario_id} | "
            f"Tamaño: {nueva_entrada.tamanio} bytes | "
            f"Líneas: {nueva_entrada.lineas}"
        )
        
        # Respuesta exitosa
        return jsonify({
            "success": True,
            "message": f"Script almacenado correctamente para el usuario {usuario_id}",
            "usuario_id": usuario_id,
            "tamano_bytes": nueva_entrada.tamanio,
            "lineas": nueva_entrada.lineas,
            "timestamp": time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime()),
            "expira_en": f"{SCRIPT_TIMEOUT} segundos"
        }), 200
        
    except Exception as e:
        logger.error(f"💥 Error en /enviar-script: {str(e)}", exc_info=True)
        return jsonify({
            "error": "Error interno del servidor",
            "detalle": str(e) if app.debug else "Contacta al administrador"
        }), 500


@app.route('/obtener-script', methods=['GET'])
def obtener_script():
    """
    Endpoint para que el loader de Roblox obtenga scripts pendientes
    
    Parámetros GET:
    - usuario: ID del usuario de Roblox
    
    Retorna:
    - El código en texto plano si existe
    - "ninguno" si no hay scripts pendientes
    """
    try:
        # Obtener parámetro de la URL
        usuario_param = request.args.get('usuario')
        
        # Validar que el parámetro existe
        if not usuario_param:
            logger.warning("📭 Petición sin parámetro 'usuario'")
            return Response(
                "Error: Parámetro 'usuario' requerido. Ejemplo: /obtener-script?usuario=123",
                mimetype='text/plain',
                status=400
            )
        
        # Validar que sea un número válido
        try:
            usuario_id = int(usuario_param)
        except ValueError:
            logger.warning(f"❌ Parámetro 'usuario' no numérico: {usuario_param}")
            return Response(
                "Error: El parámetro 'usuario' debe ser un número entero",
                mimetype='text/plain',
                status=400
            )
        
        # Validar rango
        if usuario_id <= 0:
            return Response(
                "Error: ID de usuario inválido",
                mimetype='text/plain',
                status=400
            )
        
        # ============================================================
        # BUSCAR Y SERVIR SCRIPT
        # ============================================================
        
        with scripts_lock:
            if usuario_id in pending_scripts:
                entrada = pending_scripts[usuario_id]
                
                # Incrementar contador de intentos
                entrada.intentos += 1
                
                # Obtener el código
                codigo = entrada.codigo
                
                # ELIMINAR después de servir (one-time use)
                del pending_scripts[usuario_id]
                
                logger.info(
                    f"📤 Script servido | "
                    f"Usuario: {usuario_id} | "
                    f"Tamaño: {len(codigo)} bytes | "
                    f"Intento #{entrada.intentos}"
                )
                
                # Devolver código como texto plano
                return Response(
                    codigo,
                    mimetype='text/plain; charset=utf-8',
                    status=200,
                    headers={
                        'Cache-Control': 'no-cache, no-store, must-revalidate',
                        'Pragma': 'no-cache',
                        'Expires': '0'
                    }
                )
            else:
                # No hay scripts para este usuario
                logger.debug(f"🔍 Sin scripts pendientes para usuario {usuario_id}")
                return Response(
                    'ninguno',
                    mimetype='text/plain',
                    status=200
                )
                
    except Exception as e:
        logger.error(f"💥 Error en /obtener-script: {str(e)}", exc_info=True)
        return Response(
            f"Error interno del servidor: {str(e)}",
            mimetype='text/plain',
            status=500
        )


@app.route('/', methods=['GET'])
def index():
    """Ruta raíz - Información del servicio"""
    with scripts_lock:
        scripts_pendientes = len(pending_scripts)
    
    return jsonify({
        "servicio": "Nebula Hub API",
        "version": "2.0.0",
        "endpoints": {
            "health": "/health",
            "enviar_script": "/enviar-script [POST]",
            "obtener_script": "/obtener-script?usuario=ID [GET]"
        },
        "scripts_pendientes": scripts_pendientes,
        "documentacion": "https://github.com/tu-usuario/nebula-hub"
    }), 200


# ============================================================
# MANEJO DE ERRORES HTTP
# ============================================================

@app.errorhandler(400)
def bad_request(error):
    return jsonify({
        "error": "Petición incorrecta",
        "detalle": str(error)
    }), 400

@app.errorhandler(404)
def not_found(error):
    return jsonify({
        "error": "Ruta no encontrada",
        "endpoints_disponibles": ["/", "/health", "/enviar-script", "/obtener-script"]
    }), 404

@app.errorhandler(413)
def too_large(error):
    return jsonify({
        "error": "Payload demasiado grande",
        "limite": "1 MB"
    }), 413

@app.errorhandler(500)
def internal_error(error):
    return jsonify({
        "error": "Error interno del servidor"
    }), 500

# ============================================================
# PUNTO DE ENTRADA PARA RENDER
# ============================================================

if __name__ == '__main__':
    # Obtener puerto de la variable de entorno (Render lo asigna automáticamente)
    port = int(os.environ.get('PORT', 5000))
    
    # Modo debug solo en desarrollo local
    debug_mode = os.environ.get('FLASK_ENV', 'production') == 'development'
    
    logger.info("=" * 60)
    logger.info("🔥 NEBULA HUB API v2.0 - Iniciando servidor")
    logger.info(f"📡 Puerto: {port}")
    logger.info(f"🐛 Modo Debug: {debug_mode}")
    logger.info(f"🌐 CORS: Habilitado para todos los orígenes")
    logger.info("=" * 60)
    
    # Iniciar servidor
    # Render usa Gunicorn en producción, esto es solo para desarrollo local
    app.run(
        host='0.0.0.0',
        port=port,
        debug=debug_mode
)
