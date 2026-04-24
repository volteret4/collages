import re

def limpiar_agresivo(texto):
    """Elimina etiquetas comunes de YouTube, paréntesis y ruido visual."""
    if not texto:
        return ""

    # 1. Pasar a minúsculas
    texto = texto.lower()

    # 2. Lista de términos a eliminar (los que mencionaste y extras comunes)
    ruido = [
        r'\(remastered 2009\)', r'\(full audio\)', r'\(official video cover\)',
        r'\(full album\)', r'\(official lyric video\)', r'\(audio\)',
        r'\(1080 60fps\)', r'\(remastered\)', r'\(official video\)',
        r'\(official music video\)', r'\[.*?\]',  # Elimina todo lo que esté entre corchetes []
        r'video oficial', r'lyric video', r'full hd', r'4k', r'hq'
    ]

    for patron in ruido:
        texto = re.sub(patron, '', texto)

    # 3. Eliminar paréntesis restantes y su contenido para mayor seguridad
    texto = re.sub(r'\(.*?\)', '', texto)

    # 4. Limpiar caracteres especiales y espacios múltiples
    texto = re.sub(r'[^a-z0-9áéíóúñ\s\-]', '', texto)
    texto = " ".join(texto.split())

    return texto.strip()

def comparar_listas(archivo_yt, archivo_clean):
    try:
        with open(archivo_yt, "r", encoding="utf-8") as f:
            videos_raw = [line.strip() for line in f if line.strip()]

        with open(archivo_clean, "r", encoding="utf-8") as f:
            canciones_clean = [limpiar_agresivo(line) for line in f if line.strip()]

        print(f"--- Procesando {len(videos_raw)} videos ---")

        encontrados = 0
        no_encontrados = []

        for v_original in videos_raw:
            v_procesado = limpiar_agresivo(v_original)

            # Buscamos si el video procesado contiene alguna de nuestras canciones o viceversa
            match = False
            for c_clean in canciones_clean:
                if c_clean in v_procesado or v_procesado in c_clean:
                    match = True
                    break

            if match:
                encontrados += 1
            else:
                no_encontrados.append(v_original)

        print(f"Coincidencias encontradas: {encontrados}")
        print(f"No encontrados: {len(no_encontrados)}")
        print("-" * 30)

        if no_encontrados:
            print("EJEMPLOS DE VIDEOS SIN PAREJA (Aquí estará tu video extra):")
            # Mostramos los primeros 10 para no saturar, pero el extra estará aquí
            for v in no_encontrados[:20]:
                print(f"❌ {v}")

    except FileNotFoundError as e:
        print(f"Error: No se encontró el archivo - {e}")

if __name__ == "__main__":
    # Asegúrate de poner aquí los nombres exactos de tus archivos
    comparar_listas("list.txt", "custom_txt_collage/txt/rym_beautiful_songs.txt")
