"""
Launcher pentru ASP Exam Checker
Acest script este convertit in .exe si lanseaza scrapper.py cu Python
"""
import subprocess
import sys
import os

def main():
    # Gaseste scrapper.py in acelasi folder cu .exe-ul
    if getattr(sys, 'frozen', False):
        # Rulam ca .exe
        base_dir = os.path.dirname(sys.executable)
    else:
        base_dir = os.path.dirname(os.path.abspath(__file__))

    script_path = os.path.join(base_dir, 'scrapper.py')

    if not os.path.exists(script_path):
        print("EROARE: scrapper.py nu a fost gasit in acelasi folder cu .exe-ul!")
        print(f"Cauta in: {base_dir}")
        input("Apasa ENTER pentru a inchide...")
        return

    # Lanseaza scrapper.py cu Python
    result = subprocess.run([sys.executable if not getattr(sys, 'frozen', False) else 'python', script_path])

if __name__ == "__main__":
    main()