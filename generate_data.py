"""
Generates mock registration, attendance, and feedback data on top of events.csv,
with a few *intentional* gaps so the agent's missing-data handling has something
real to demonstrate:
  - E06 (Drone Building Bootcamp)  -> no attendance records at all
  - E12 (Bug Bounty Bootcamp)      -> no attendance records at all
  - E03, E09, E14                  -> zero feedback responses
  - AI/ML Club                     -> registrations exist but are unusually sparse for E09
"""
import csv
import random

random.seed(42)

events = list(csv.DictReader(open("data/events.csv")))

# Base "popularity" per event (registration pool size)
reg_pool = {
    "E01": 70, "E02": 95, "E03": 60,
    "E04": 55, "E05": 80, "E06": 50,
    "E07": 65, "E08": 90, "E09": 40,
    "E10": 75, "E11": 60, "E12": 45,
    "E13": 50, "E14": 35,
}

# Events with NO attendance data captured (simulating organizers forgetting to log it)
NO_ATTENDANCE = {"E06", "E12"}
# Events with NO feedback responses submitted
NO_FEEDBACK = {"E03", "E09", "E14"}

registrations, attendance, feedback = [], [], []
student_counter = 1

for ev in events:
    eid = ev["event_id"]
    n_reg = reg_pool[eid]
    reg_ids = []
    for _ in range(n_reg):
        sid = f"S{student_counter:04d}"
        student_counter += 1
        reg_ids.append(sid)
        registrations.append({"event_id": eid, "student_id": sid, "registered_at": ev["date"]})

    if eid not in NO_ATTENDANCE:
        # attendance rate varies by club to create real ranking differences
        rate = {
            "Coding Club": 0.82, "Robotics Club": 0.88, "AI/ML Club": 0.75,
            "Cybersecurity Club": 0.70, "IoT Club": 0.78,
        }[ev["club_name"]]
        rate += random.uniform(-0.06, 0.06)
        attendees = random.sample(reg_ids, k=max(1, int(n_reg * rate)))
        for sid in attendees:
            attendance.append({"event_id": eid, "student_id": sid, "attended": 1})

        if eid not in NO_FEEDBACK:
            base_rating = {
                "Coding Club": 4.3, "Robotics Club": 4.6, "AI/ML Club": 4.0,
                "Cybersecurity Club": 3.8, "IoT Club": 4.1,
            }[ev["club_name"]]
            # not everyone who attends leaves feedback
            responders = random.sample(attendees, k=max(1, int(len(attendees) * 0.55)))
            for sid in responders:
                rating = max(1, min(5, round(random.gauss(base_rating, 0.6))))
                feedback.append({"event_id": eid, "student_id": sid, "rating": rating})

with open("data/registrations.csv", "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=["event_id", "student_id", "registered_at"])
    w.writeheader(); w.writerows(registrations)

with open("data/attendance.csv", "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=["event_id", "student_id", "attended"])
    w.writeheader(); w.writerows(attendance)

with open("data/feedback.csv", "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=["event_id", "student_id", "rating"])
    w.writeheader(); w.writerows(feedback)

print(f"registrations: {len(registrations)} rows")
print(f"attendance:    {len(attendance)} rows  (missing for events: {sorted(NO_ATTENDANCE)})")
print(f"feedback:      {len(feedback)} rows  (missing for events: {sorted(NO_FEEDBACK)})")
