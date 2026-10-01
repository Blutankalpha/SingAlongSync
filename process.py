import os
import re
import json
import subprocess
import sys
import shutil
import requests
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from yt_dlp import YoutubeDL
import whisperx
import torch

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.align import Align

# --------------------------------------------------
# CONFIG & DEVICE
# --------------------------------------------------
DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
DOWNLOADS_BASE = Path("downloads")
DOWNLOADS_BASE.mkdir(exist_ok=True)

ELRC_SCRIPT = Path(__file__).resolve().parent / "SAS_to_elrc.py"

console = Console()

# --------------------------------------------------
# UTILS
# --------------------------------------------------
def extract_video_id(url):
    parsed = urlparse(url)
    if "youtube.com" in parsed.netloc:
        query = parse_qs(parsed.query)
        if "v" in query:
            return query["v"][0]
    if "youtu.be" in parsed.netloc:
        return parsed.path[1:]
    return None

class KaraokePipeline:
    def __init__(self, url):
        self.url = url
        self.video_id = extract_video_id(url)
        if not self.video_id:
            raise ValueError("Invalid YouTube URL")

        self.work_dir = DOWNLOADS_BASE / self.video_id
        self.work_dir.mkdir(exist_ok=True)

        self.status_path = self.work_dir / "status.json"
        self.metadata_path = self.work_dir / "metadata.json"
        self.audio_path = self.work_dir / "audio.mp3"
        self.lrc_path = self.work_dir / "synced.lrc"
        self.karaoke_path = self.work_dir / "karaoke.json"
        self.elrc_path = self.work_dir / "karaoke.elrc"
        self.vocals_path = self.work_dir / "vocals.wav"
        self.instrumental_path = self.work_dir / "instrumental.wav"

    def get_status(self):
        if not self.status_path.exists():
            return {}
        with open(self.status_path, "r") as f:
            return json.load(f)

    def save_status(self, updates):
        status = self.get_status()
        status.update(updates)
        with open(self.status_path, "w") as f:
            json.dump(status, f, indent=2)

    def process(self):
        console.print(Panel(
            Align.center(f"[bold magenta]🎤 SingAlongSync CLI 🎤[/bold magenta]\n[cyan]Video ID: {self.video_id}[/cyan]"),
            border_style="magenta",
            title="[bold yellow]Processing Startup[/bold yellow]"
        ))

        # 1. Check Cache
        if self.karaoke_path.exists():
            if not self.elrc_path.exists():
                self.elrc_step()
            console.print(Panel(
                f"[bold green]✨ Already fully processed! Skipping. ✨[/bold green]\n"
                f"[dim]Output files located in: [italic]{self.work_dir}[/italic][/dim]",
                border_style="green",
                title="[bold green]Cache Hit[/bold green]"
            ))
            self._print_summary_table()
            return

        # 2. Download Audio & Metadata
        if not self.audio_path.exists() or not self.metadata_path.exists():
            self.download_step()

        # 3. Fetch Lyrics
        if not self.lrc_path.exists():
            self.fetch_lyrics_step()

        # 4. Separate Vocals (Demucs)
        if not self.vocals_path.exists():
            self.separate_step()

        # 5. Align (WhisperX)
        if not self.karaoke_path.exists():
            self.align_step()

        # 6. Convert to Enhanced LRC (SAS_to_elrc.py)
        if not self.elrc_path.exists():
            self.elrc_step()

        console.print(Panel(
            Align.center("[bold green]🌟 Pipeline Successfully Completed! 🌟[/bold green]\n[cyan]All audio stems and timing data have been built and saved.[/cyan]"),
            border_style="green",
            title="[bold green]Success[/bold green]"
        ))
        self._print_summary_table()

    def download_step(self):
        self.save_status({"stage": "downloading", "progress": 0})

        ydl_opts = {
            "format": "bestaudio/best",
            "outtmpl": str(self.work_dir / "audio.%(ext)s"),
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "320",
            }],
            "writethumbnail": True,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
        }

        with console.status("[bold cyan]📥 Downloading audio & metadata via yt-dlp...[/bold cyan]", spinner="dots") as status:
            try:
                with YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(self.url, download=True)

                    # Save metadata
                    metadata = {
                        "videoId": self.video_id,
                        "title": info.get("track") or info.get("title"),
                        "artist": info.get("artist") or info.get("uploader"),
                        "album": info.get("album") or "",
                        "duration": round(info.get("duration", 0)),
                        "thumbnail": info.get("thumbnail")
                    }
                    with open(self.metadata_path, "w", encoding="utf-8") as f:
                        json.dump(metadata, f, indent=2)

                    self.save_status({"downloaded": True, "stage": "metadata_saved"})

                console.print(Panel(
                    f"[bold green]✓ Download Completed Successfully![/bold green]\n"
                    f"[yellow]Title:[/yellow] {metadata['title']}\n"
                    f"[yellow]Artist:[/yellow] {metadata['artist']}\n"
                    f"[yellow]Duration:[/yellow] {metadata['duration']} seconds",
                    border_style="green",
                    title="[bold green]Step 1: Download Complete[/bold green]"
                ))
            except Exception as e:
                console.print(f"[bold red]✗ Download failed: {e}[/bold red]")
                raise e

    def fetch_lyrics_step(self):
        self.save_status({"stage": "fetching_lyrics"})
        with open(self.metadata_path, "r") as f:
            meta = json.load(f)

        search_url = "https://lrclib.net/api/search"
        params = {"q": f"{meta['title']} {meta['artist']}"}
        headers = {"User-Agent": "karaoke-sync-app/1.0"}

        with console.status(f"[bold magenta]🔍 Fetching synced lyrics for '[italic]{meta['title']}[/italic]'...[/bold magenta]", spinner="dots") as status:
            try:
                response = requests.get(search_url, params=params, headers=headers)
                results = response.json()

                if not results:
                    console.print(Panel(
                        "[bold yellow]⚠ No lyrics found on LRCLIB.[/bold yellow]\n"
                        "[dim]Proceeding without lyric alignment steps.[/dim]",
                        border_style="yellow",
                        title="[bold yellow]Step 2: No Lyrics[/bold yellow]"
                    ))
                    self.save_status({"lyrics_found": False})
                    return

                # Best duration match
                best_match = min(results, key=lambda x: abs(x.get("duration", 0) - meta["duration"]))

                if abs(best_match.get("duration", 0) - meta["duration"]) > 5:
                    console.print(Panel(
                        "[bold yellow]⚠ No close duration match found on LRCLIB.[/bold yellow]\n"
                        "[dim]Proceeding without lyric alignment steps.[/dim]",
                        border_style="yellow",
                        title="[bold yellow]Step 2: No Match[/bold yellow]"
                    ))
                    self.save_status({"lyrics_found": False})
                    return

                synced_lyrics = best_match.get("syncedLyrics")
                if synced_lyrics:
                    with open(self.lrc_path, "w", encoding="utf-8") as f:
                        f.write(synced_lyrics)
                    self.save_status({"lyrics_found": True})

                    parsed_lines = self._parse_lrc(self.lrc_path)
                    preview_text = ""
                    if parsed_lines:
                        preview_items = [f"  [italic]“{line['text']}”[/italic]" for line in parsed_lines[:3] if line['text'].strip()]
                        if preview_items:
                            preview_text = "\n\n[yellow]Lyrics Preview:[/yellow]\n" + "\n".join(preview_items)

                    console.print(Panel(
                        f"[bold green]✓ Lyrics Fetched & Synced![/bold green]\n"
                        f"[dim]Saved synced lyrics (.lrc) successfully.[/dim]{preview_text}",
                        border_style="green",
                        title="[bold green]Step 2: Lyrics Fetched[/bold green]"
                    ))
                else:
                    console.print(Panel(
                        "[bold yellow]⚠ Synced lyrics not available in the best match.[/bold yellow]",
                        border_style="yellow",
                        title="[bold yellow]Step 2: No Synced Lyrics[/bold yellow]"
                    ))
                    self.save_status({"lyrics_found": False})

            except Exception as e:
                console.print(f"[bold red]✗ Lyrics search failed: {e}[/bold red]")
                self.save_status({"lyrics_found": False})

    def separate_step(self):
        self.save_status({"stage": "separating_vocals"})

        console.print(Panel(
            "[bold yellow]⚡ Splitting Audio into Vocals & Instrumentals (Demucs)...[/bold yellow]\n"
            "[dim]Running Demucs high-quality stem separation. This uses heavy AI computation.[/dim]",
            border_style="yellow",
            title="[bold yellow]Step 3: Vocal Separation[/bold yellow]"
        ))

        cmd = [
            "uv", "run", "demucs",
            "-n", "htdemucs",
            "--two-stems=vocals",
            str(self.audio_path)
        ]

        subprocess.run(cmd, check=True)

        sep_dir = Path("separated") / "htdemucs" / "audio"

        if (sep_dir / "vocals.wav").exists():
            shutil.move(str(sep_dir / "vocals.wav"), str(self.vocals_path))
            shutil.move(str(sep_dir / "no_vocals.wav"), str(self.instrumental_path))
            # Cleanup the empty demucs folder
            shutil.rmtree("separated")
            self.save_status({"separated": True, "stage": "separation_complete"})
            console.print(Panel(
                "[bold green]✓ Audio successfully split into Vocals and Instrumental stems![/bold green]\n"
                f"[yellow]Vocals:[/yellow] {self.vocals_path.name}\n"
                f"[yellow]Instrumental:[/yellow] {self.instrumental_path.name}",
                border_style="green",
                title="[bold green]Step 3 Complete[/bold green]"
            ))
        else:
            console.print("[bold red]✗ Separation failed: Output files not found where expected.[/bold red]")
            raise FileNotFoundError("Demucs output files not found.")

    def align_step(self):
        if not self.lrc_path.exists():
            console.print("[yellow]⚠ Skipping Step 4: No synced lyrics (.lrc) available for alignment.[/yellow]")
            return

        self.save_status({"stage": "aligning_lyrics"})

        console.print(Panel(
            "[bold blue]🎙 Aligning Synced Lyrics (WhisperX)...[/bold blue]\n"
            "[dim]Matching vocal recordings with lyrics text down to the word level.[/dim]",
            border_style="blue",
            title="[bold blue]Step 4: Lyric Alignment[/bold blue]"
        ))

        # Parse LRC
        lrc_lines = self._parse_lrc(self.lrc_path)
        segments = [
            {"start": line["start"], "end": line["end"], "text": line["text"]}
            for line in lrc_lines if line["text"].strip()
        ]

        with console.status("[bold blue]Loading WhisperX model and alignment weights...[/bold blue]", spinner="aesthetic") as status:
            audio = whisperx.load_audio(str(self.vocals_path))
            status.update("[bold blue]Initializing English alignment model...[/bold blue]")
            model_a, metadata = whisperx.load_align_model(language_code="en", device=DEVICE)

            status.update("[bold blue]Executing millisecond-level word alignment...[/bold blue]")
            result = whisperx.align(segments, model_a, metadata, audio, DEVICE)

        karaoke_data = []
        for segment in result["segments"]:
            words = []
            for word in segment.get("words", []):
                start = word.get("start")
                end = word.get("end")
                if start is None or end is None: continue
                words.append({
                    "word": word["word"].strip(),
                    "start": round(start, 3),
                    "end": round(end, 3)
                })

            karaoke_data.append({
                "line": segment["text"],
                "start": round(segment["start"], 3),
                "end": round(segment["end"], 3),
                "words": words
            })

        with open(self.karaoke_path, "w", encoding="utf-8") as f:
            json.dump(karaoke_data, f, indent=2, ensure_ascii=False)

        self.save_status({"aligned": True, "stage": "complete"})

        console.print(Panel(
            f"[bold green]✓ Word-level lyric alignment successfully completed![/bold green]\n"
            f"[dim]Generated timings for [yellow]{len(karaoke_data)}[/yellow] lyric lines.[/dim]",
            border_style="green",
            title="[bold green]Step 4 Complete[/bold green]"
        ))

    def elrc_step(self):
        if not self.karaoke_path.exists():
            console.print("[yellow]⚠ Skipping Step 5: No aligned timing data (karaoke.json) to convert.[/yellow]")
            return

        if not ELRC_SCRIPT.exists():
            console.print(f"[bold red]✗ Cannot find {ELRC_SCRIPT.name} next to process.py.[/bold red]")
            return

        self.save_status({"stage": "converting_elrc"})

        try:
            subprocess.run(
                [sys.executable, str(ELRC_SCRIPT), str(self.karaoke_path), str(self.elrc_path)],
                check=True,
            )
        except subprocess.CalledProcessError as e:
            console.print(f"[bold red]✗ ELRC conversion failed: {e}[/bold red]")
            return

        self.save_status({"elrc": True, "stage": "complete"})
        console.print(Panel(
            f"[bold green]✓ Enhanced LRC generated![/bold green]\n"
            f"[yellow]File:[/yellow] {self.elrc_path.name}",
            border_style="green",
            title="[bold green]Step 5 Complete[/bold green]"
        ))

    def _parse_lrc(self, path):
        pattern = r"\[(\d+):(\d+\.\d+)\](.*)"
        lines = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                match = re.match(pattern, line.strip())
                if not match: continue
                timestamp = int(match.group(1)) * 60 + float(match.group(2))
                lines.append({"start": timestamp, "text": match.group(3).strip()})

        for i in range(len(lines) - 1):
            lines[i]["end"] = lines[i + 1]["start"]
        if lines:
            lines[-1]["end"] = lines[-1]["start"] + 5
        return lines

    def _print_summary_table(self):
        table = Table(title="Generated Outputs Summary", border_style="cyan")
        table.add_column("File Type", style="bold yellow")
        table.add_column("File Name", style="white")
        table.add_column("Status", style="bold green")
        table.add_column("Size", style="dim")

        files_to_check = [
            ("Original Audio", self.audio_path),
            ("Metadata JSON", self.metadata_path),
            ("Synced LRC", self.lrc_path),
            ("Separated Vocals", self.vocals_path),
            ("Instrumental Track", self.instrumental_path),
            ("Aligned Timing Data", self.karaoke_path),
            ("Enhanced LRC", self.elrc_path),
        ]

        for desc, path in files_to_check:
            if path.exists():
                size_kb = path.stat().st_size / 1024
                size_str = f"{size_kb:.1f} KB" if size_kb < 1024 else f"{size_kb/1024:.2f} MB"
                table.add_row(desc, path.name, "✓ Found", size_str)
            else:
                table.add_row(desc, path.name, "✗ Missing", "-", style="dim red")

        console.print(table)

if __name__ == "__main__":
    # Beautiful welcome header
    console.print(Panel(
        Align.center("[bold magenta]🎤 Welcome to SingAlongSync 🎤[/bold magenta]\n[cyan]Separate vocals & align word-level timings with deep-learning precision.[/cyan]"),
        border_style="magenta"
    ))

    if len(sys.argv) > 1:
        url = sys.argv[1]
    else:
        url = console.input("[bold yellow]🔗 Enter YouTube URL:[/bold yellow] ")

    try:
        pipeline = KaraokePipeline(url)
        pipeline.process()
    except Exception as e:
        console.print(Panel(
            f"[bold red]FATAL ERROR: {e}[/bold red]",
            border_style="red",
            title="[bold red]Execution Failed[/bold red]"
        ))
