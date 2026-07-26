import sqlite3
import requests
import time
import sys


from datetime import datetime, timezone



# =====================================================
# CONFIG
# =====================================================


TOKEN = "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"

MY_USERNAME = "Lewickiy"


DAILY_FOLLOW_LIMIT = 90


FOLLOW_DELAY = 60


DATABASE = "github_social.db"



INTERESTS = {

    "languages": [
        "Java",
        "Python",
        "Kotlin"
    ],

    "topics": [
        "robotics",
        "simulation",
        "warehouse",
        "automation",
        "logistics",
        "ecs"
    ]
}



API = "https://api.github.com"


HEADERS = {

    "Authorization":
        f"Bearer {TOKEN}",

    "Accept":
        "application/vnd.github+json"

}




# =====================================================
# DATABASE
# =====================================================


class Database:


    def __init__(self):

        self.conn = sqlite3.connect(
            DATABASE
        )


        self.init()



    def init(self):

        c=self.conn.cursor()


        c.execute("""
        CREATE TABLE IF NOT EXISTS users
        (

            username TEXT PRIMARY KEY,

            discovered_from TEXT,


            score INTEGER DEFAULT 0,


            public_repos INTEGER DEFAULT 0,

            followers INTEGER DEFAULT 0,


            bio TEXT,

            company TEXT,


            status TEXT DEFAULT 'NEW',


            created_at TEXT,

            scored_at TEXT,

            followed_at TEXT

        )
        """)



        c.execute("""
        CREATE TABLE IF NOT EXISTS actions
        (
            id INTEGER PRIMARY KEY,

            username TEXT,

            action TEXT,

            created_at TEXT
        )
        """)


        self.conn.commit()



    def add_user(
        self,
        username,
        source
    ):


        self.conn.execute("""
        INSERT OR IGNORE INTO users
        (
            username,
            discovered_from,
            created_at
        )
        VALUES (?, ?, ?)
        """,
        (
            username,
            source,
            datetime.now(
                timezone.utc
            ).isoformat()
        ))


        self.conn.commit()



    def update_score(
        self,
        username,
        score,
        info
    ):


        self.conn.execute("""
        UPDATE users

        SET

        score=?,

        public_repos=?,

        followers=?,

        bio=?,

        company=?,

        scored_at=?

        WHERE username=?

        """,
        (

            score,

            info.get(
                "public_repos",
                0
            ),

            info.get(
                "followers",
                0
            ),

            info.get(
                "bio"
            ),

            info.get(
                "company"
            ),

            datetime.now(
                timezone.utc
            ).isoformat(),

            username
        ))


        self.conn.commit()



    def top_users(
        self,
        limit
    ):


        rows=self.conn.execute("""
        SELECT username,score

        FROM users

        WHERE status='NEW'

        ORDER BY score DESC

        LIMIT ?

        """,
        (
            limit,
        )).fetchall()



        return rows



    def mark_followed(
        self,
        username
    ):


        now=datetime.now(
            timezone.utc
        ).isoformat()


        self.conn.execute("""
        UPDATE users

        SET

        status='FOLLOWED',

        followed_at=?

        WHERE username=?

        """,
        (
            now,
            username
        ))



        self.conn.execute("""
        INSERT INTO actions
        (
            username,
            action,
            created_at
        )

        VALUES
        (?, ?, ?)

        """,
        (
            username,
            "FOLLOW",
            now
        ))



        self.conn.commit()



    def today_follows(self):

        return self.conn.execute("""
        SELECT COUNT(*)

        FROM actions

        WHERE action='FOLLOW'

        AND date(created_at)
        =
        date('now')

        """).fetchone()[0]






# =====================================================
# GITHUB CLIENT
# =====================================================



class GithubClient:



    def request(
        self,
        method,
        url,
        **kwargs
    ):


        r=requests.request(

            method,

            API+url,

            headers=HEADERS,

            **kwargs
        )


        if r.status_code >=400:

            print(
                "GitHub error",
                r.status_code,
                url
            )

            return None


        if r.text:

            return r.json()


        return True



    def followers(
        self,
        username
    ):


        result=[]


        page=1


        while True:


            data=self.request(
                "GET",

                f"/users/{username}/followers",

                params={
                    "per_page":100,
                    "page":page
                }
            )


            if not data:

                break


            result.extend(data)


            page+=1



        return result



    def user(
        self,
        username
    ):


        return self.request(
            "GET",
            f"/users/{username}"
        )



    def follow(
        self,
        username
    ):


        r=requests.put(

            f"{API}/user/following/{username}",

            headers=HEADERS

        )


        return r.status_code==204



    def already_following(
        self,
        username
    ):


        r=requests.get(

            f"{API}/user/following/{username}",

            headers=HEADERS

        )


        return r.status_code==204







# =====================================================
# COLLECTOR
# =====================================================



class Collector:



    def __init__(
        self,
        db,
        github
    ):

        self.db=db

        self.github=github



    def run(self):


        print(
            "Collecting graph..."
        )


        people=self.github.followers(
            MY_USERNAME
        )


        for person in people:


            source=person["login"]


            print(
                "scan",
                source
            )


            for user in self.github.followers(
                source
            ):


                username=user["login"]


                if username != MY_USERNAME:


                    self.db.add_user(

                        username,

                        source

                    )


            time.sleep(1)







# =====================================================
# SCORER
# =====================================================



class Scorer:



    def __init__(
        self,
        db,
        github
    ):

        self.db=db

        self.github=github



    def calculate(
        self,
        info
    ):


        score=0



        repos=info.get(
            "public_repos",
            0
        )


        followers=info.get(
            "followers",
            0
        )


        if repos>5:

            score+=5


        if repos>20:

            score+=10



        if followers>20:

            score+=5


        if followers>100:

            score+=10



        if info.get(
            "bio"
        ):

            score+=5



        return score




    def run(self):


        rows=self.db.conn.execute("""
        SELECT username

        FROM users

        WHERE score=0

        """).fetchall()



        for row in rows:


            username=row[0]


            print(
                "Scoring",
                username
            )


            info=self.github.user(
                username
            )


            if info:


                score=self.calculate(
                    info
                )


                self.db.update_score(

                    username,

                    score,

                    info

                )


            time.sleep(1)







# =====================================================
# FOLLOW ENGINE
# =====================================================



class FollowEngine:



    def __init__(
        self,
        db,
        github
    ):

        self.db=db

        self.github=github




    def run(self):


        done=self.db.today_follows()


        left=DAILY_FOLLOW_LIMIT-done



        print(
            "Available:",
            left
        )


        if left<=0:

            return



        users=self.db.top_users(
            left
        )



        for username,score in users:


            print(
                "FOLLOW",
                username,
                "score",
                score
            )


            if not self.github.already_following(
                username
            ):


                if self.github.follow(
                    username
                ):

                    self.db.mark_followed(
                        username
                    )



            time.sleep(
                FOLLOW_DELAY
            )







# =====================================================
# CLI
# =====================================================



def main():


    db=Database()

    github=GithubClient()



    if "--collect" in sys.argv:

        Collector(
            db,
            github
        ).run()



    elif "--score" in sys.argv:


        Scorer(
            db,
            github
        ).run()



    elif "--follow" in sys.argv:


        FollowEngine(
            db,
            github
        ).run()



    elif "--top" in sys.argv:


        for user,score in db.top_users(50):

            print(
                user,
                score
            )


    else:

        print(
"""
Usage:

python github_social_agent.py --collect

python github_social_agent.py --score

python github_social_agent.py --follow

python github_social_agent.py --top

"""
        )




if __name__=="__main__":

    main()