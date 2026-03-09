# Signal Scanner v8 — Smart Money Edition (Deriv Forex)

Este repositorio contiene la lógica de trading automatizado de Forex (con foco en `GBP/USD`, `GBP/JPY` y `XAG/USD`) a través de la red de websockets de **Deriv**. El proyecto se configura y optimiza específicamente para el *trading basado en divergencias RSI*, gestionando el riesgo a través de paradas de rastreo dinámicas adaptativas a la volatilidad.

## Archivos Principales

- `signal_scanner.py`: El motor asíncrono que corre la estrategia técnica. Mantiene la conexión vía WebSocket al entorno productivo de **Deriv**, administra los hilos de suscripción de ticks y maneja cada tick evaluando condiciones del indicador (RSI + MACD/Fractales como confirmación).
- `shared.py`: Biblioteca de estado con utilitarios complementarios, como tracking de P&L, limitadores de stop de pérdida diarios, conexión con Telegram (dashboard resúmenes) y el filtro fundamental (Noticias vía Finnhub). 

## Estrategia de Operación (RSI Divergence)

El bot escanea los siguientes pares con parámetros de sensibilidad ajustados. Las entradas solo se lanzan si ocurren a favor de la Media Móvil Exponencial principal de **1 Hora** (`use_h1_inertia = True`), una métrica que protege el capital contra retrocesos de ruido que van en la dirección equivocada de la tendencia macro.

### 1. GBP/USD (El Cable)
- **Marco Temporal Central**: M15 (Velas de 15 minutos).
- **Ventana de Ejecución**: Operativa restingida la intersección de más alta liquidez (`london_newyork_overlap`), desde las 12:00 UTC hasta las 16:00 UTC.
- **Riesgo y Cierre**: Usa el trailing "Optimized", moviendo el Stop a 1.5 veces el `ATR` (Rango Promedio Real).

### 2. GBP/JPY (The Beast)
- **Marco Temporal Central**: M30 (Velas de 30 minutos). Se eliminó la inestabilidad de M15 para limpiar señales falsas en un par que fluctúa hasta 150 pips al día.
- **Ventana de Ejecución**: Intersección entre la apertura de Londres y el cierre en Asia (`asia_london_overlap`): 6:00 UTC a 9:00 UTC.
- **Riesgo y Cierre**: Su Trailing Stop respira mucho más holgadamente mediante 2.5 `ATR`, dando el espacio suficiente para sobrevivir a picos erráticos bruscos (spikes).

### 3. XAG/USD (Silver/Plata frente al Dólar)
- **Marco Temporal Central**: M15
- **Ventana de Ejecución**: Todo el día. 
- **Riesgo y Cierre**: Sensibilidad conservadora gracias a su conexión fuerte ante indicadores DXY del dólar. Trailing Stop usando un factor de arrastre `ATR * 2.0`.

## Modalidad de Cierre de Contratos (Trailing: Optimized)

Este bot ya no utiliza "Take Profits" estáticos (márgenes fijos). El algoritmo "persigue" infinitamente el precio a tu favor. Cuando la operación se halla en beneficio masivo, mueve el cinturón de seguridad asegurando gran cantidad de ese dinero generado. Al primer quiebre de tendencia, vende para recolectar el saldo real en tu balance.

Además, cuenta con el "Correlation Shield" de exposición en Forex, que impide que abra dos posiciones con monedas superpuestas temporalmente (evitando un sobre-exposición, ej. a una caída simultánea del GBP provocado por una noticia no contemplada).

## Disclaimer sobre Noticias
Existe un freno por noticias macroeconómicas (Finnhub) dentro de `shared.py`, el cual previene entrar 30 minutos antes y salir 15 minutos posteriores a eventos "High Impact" filtrados según la divisa analizada.

Para iniciar: `python3 signal_scanner.py` (Se requerirá el token Bearer en variables de entorno: `DERIV_API_TOKEN`).
