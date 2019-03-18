import math
import random
from operator import itemgetter


class block:

    def __init__(self, Name, X, Y, Value):
        self.name = Name
        self.value = Value
        self.x = X
        self.y = Y

    def getX(self):
        return self.x

    def getY(self):
        return self.y

    def getValue(self):
        return self.value

    def setValue(self, Value):
        self.value = Value

    def getName(self):
        return self.name

    def distanceTo(self, block1):
        distX = int(math.fabs(self.getX() - block1.getX()))
        distY = int(math.fabs(self.getY() - block1.getY()))

        return math.sqrt(distX * distX + distY * distY)

    def print(self):
        print(self.getName(), "(", self.getX(), ",", self.getY(), "):", self.value)

    ################################################


class RouteManager:
    destRoutes = []

    @staticmethod
    def addBlock(block):
        RouteManager.destRoutes.append(block)

    @staticmethod
    def getBlock(index):
        return RouteManager.destRoutes.__getitem__(index)

    @staticmethod
    def numberOfBlocks():
        return RouteManager.destRoutes.__len__()

    ####################################################


class Route:

    def __init__(self, start, end, Size):
        self.route = []

        for i in range(Size):
            self.route.append(None)

        self.route[0] = start
        self.route[Size - 1] = end

        self.distance = 0
        self.fitness = 0

    def generateIndividual(self):

        for i in range(1, self.routeSize() - 1):
            tempBlock = RouteManager.getBlock(random.randint(0, RouteManager.numberOfBlocks() - 1))

            while self.containBlock(tempBlock):
                tempBlock = RouteManager.getBlock(random.randint(0, RouteManager.numberOfBlocks() - 1))

            self.setBlock(i, tempBlock)

        rest = self.route[1:self.route.__len__() - 1]
        random.shuffle(rest)
        self.route = [self.route[0]] + rest + [self.route[self.route.__len__() - 1]]

    def containBlock(self, thisBlock):

        for i in range(self.routeSize()):
            if (self.getBlock(i) != None):

                tempName = self.getBlock(i).getName()
                tempX = self.getBlock(i).getX()
                tempY = self.getBlock(i).getY()
                tempValue = self.getBlock(i).getValue()

                if (
                        tempX == thisBlock.getX() and tempY == thisBlock.getY() and tempName == thisBlock.getName() and tempValue == thisBlock.getValue()):
                    return True
        return False

    def setBlock(self, routePosition, Block):
        self.route.__setitem__(routePosition, Block)
        self.fitness = 0
        self.distance = 0

    def getBlock(self, routePosition):
        return self.route.__getitem__(routePosition)

    def routeSize(self):
        return self.route.__len__()

    def getDistance(self):

        if (self.distance == 0):

            routeDist = 0

            for i in range(self.routeSize()):

                fromBlock = self.getBlock(i)
                destBlock = self.getBlock(0)

                if (fromBlock.getValue() == "B"):
                    routeDist += 300

                if (fromBlock.getValue() == "S"):
                    routeDist += 50

                if (i + 1 < self.routeSize()):
                    destBlock = self.getBlock(i + 1)

                routeDist += int(fromBlock.distanceTo(destBlock))

            self.distance = int(routeDist)

        return self.distance

    def getFitness(self):

        if (self.fitness == 0):
            self.fitness = 1 / self.getDistance()

        return self.fitness

    def getRoute(self):
        return self.route

    def setRoute(self, routeArray):
        self.route.clear()
        self.route = []

        self.route = routeArray
        self.distance = 0
        self.fitness = 0

    def PrintMe(self):
        for i in range(self.routeSize()):
            self.getBlock(i).print()

    ###################################################


class population:

    def __init__(self, populationSize, initialise, start=0, goal=0, RouteSize=0):
        self.routes = []

        for i in range(populationSize):
            self.routes.append(None)

        if initialise:
            for i in range(self.populationSize()):
                tmpRoute = Route(start, goal, RouteSize)
                tmpRoute.generateIndividual()
                self.saveRoute(i, tmpRoute)

    def saveRoute(self, index, route1):
        self.routes.__setitem__(index, route1)

    def getRoute(self, index):
        return self.routes.__getitem__(index)

    def populationSize(self):
        return self.routes.__len__()

    def sort_on_fitness(self):
        temp = []

        for i in range(self.populationSize()):
            route = self.getRoute(i)

            temp2 = []
            temp2.append(route)
            temp2.append(route.getFitness())

            temp.append(temp2)

        temp = sorted(temp, key=itemgetter(1), reverse=True)

        for i in range(self.populationSize()):
            self.saveRoute(i, temp[i][0])

    def routeExists(self, route1):

        exists = False
        for i in range(self.populationSize()):
            route = self.getRoute(i)
            if (route != None):

                exists = True

                for j in range(1, route.routeSize() - 1):

                    bl = route.getBlock(j)
                    bl2 = route1.getBlock(j)

                    if bl.getName() == bl2.getName() and bl.getX() == bl2.getX() and bl.getY() == bl2.getY() and bl.getValue() == bl2.getValue():
                        exists = True
                    else:
                        exists = False
                        break

                if exists:
                    return exists
        return exists

    def getFittest(self):
        fittestRoute = self.getRoute(0)

        for i in range(1, self.populationSize()):

            if fittestRoute.getFitness() <= self.getRoute(i).getFitness():
                fittestRoute = self.getRoute(i)

        return fittestRoute

    #################################################


class GA:
    mutationRate = 0.015
    tournamentSize = 5
    elitism = True

    @staticmethod
    def evolvePopulation(pop):

        newPop = population(pop.populationSize() * 2, False)
        # elitismOffSet=0

        # if(GA.elitism):

        #    elitismOffSet = 1

        #    for i in range(elitismOffSet):
        #        newPop.saveRoute(i,pop.getRoute(i))

        for i in range(pop.populationSize()):
            newPop.saveRoute(i, pop.getRoute(i))

        # for i in range(elitismOffSet,newPop.populationSize()):
        for i in range(pop.populationSize(), newPop.populationSize()):

            Parent1 = GA.tournamentSelection(pop)
            Parent2 = GA.tournamentSelection(pop)

            child = GA.crossOver(Parent1, Parent2)

            while newPop.routeExists(child) is True:
                Parent1 = GA.tournamentSelection(pop)
                Parent2 = GA.tournamentSelection(pop)
                child = GA.crossOver(Parent1, Parent2)
            
            newPop.saveRoute(i, child)

        # for i in range(elitismOffSet,newPop.populationSize()):
        #    GA.mutate(newPop.getRoute(i))

        for i in range(pop.populationSize(), newPop.populationSize()):
            GA.mutate(newPop.getRoute(i))

        newPop.sort_on_fitness()
        newPop2 = population(pop.populationSize(), False)

        for i in range(pop.populationSize()):
            route = newPop.getRoute(i)
            newPop2.saveRoute(i, route)

        return newPop2

    @staticmethod
    def crossOver(route1, route2):  # 2 cut crossover

        child = Route(route1.getBlock(0), route1.getBlock(route1.routeSize() - 1), route1.routeSize())
        startPos = int(random.random() * route1.routeSize())
        endPos = int(random.random() * route2.routeSize())

        for i in range(1, child.routeSize() - 1):
            if (startPos < endPos and i > startPos and i < endPos):
                if route1.getBlock(i).getValue != "B":
                    child.setBlock(i, route1.getBlock(i))

            elif (startPos > endPos):
                if not (i < startPos and i > endPos):
                    if route1.getBlock(i).getValue != "B":
                        child.setBlock(i, route1.getBlock(i))

        for j in range(route2.routeSize()):
            if child.containBlock(route2.getBlock(j)) is False:
                for k in range(child.routeSize()):
                    if child.getBlock(k) == None:
                        child.setBlock(k, route2.getBlock(j))
                        break

        return child

    @staticmethod  # 1 cut crossover
    def crossOver2(route1, route2):

        child = Route(route1.getBlock(0), route1.getBlock(route1.routeSize() - 1), route1.routeSize())
        startPos = random.randint(1, route1.routeSize() - 1)

        for i in range(1, startPos):
            if i <= startPos:
                # if route1.getBlock(i).getValue != "B":
                child.setBlock(i, route1.getBlock(i))

        for j in range(startPos, child.routeSize()):
            if child.containBlock(route2.getBlock(j)) is False:
                for k in range(child.routeSize()):
                    if child.getBlock(k) == None:
                        child.setBlock(k, route2.getBlock(j))
                        break

        for k in range(child.routeSize()):
            if child.getBlock(k) == None:
                for j in range(startPos, child.routeSize()):
                    if child.containBlock(route1.getBlock(j)) is False:
                        child.setBlock(k, route1.getBlock(j))
                        break

        return child

    @staticmethod
    def mutate(myRoute):

        for routePos1 in range(1, myRoute.routeSize() - 1):
            if random.random() < GA.mutationRate:

                routePos2 = int(myRoute.routeSize() * random.random())
                while routePos2 == 0 or routePos2 == (myRoute.routeSize() - 1):
                    routePos2 = int(myRoute.routeSize() * random.random())

                c1 = myRoute.getBlock(routePos1)
                c2 = myRoute.getBlock(routePos2)

                myRoute.setBlock(routePos2, c1)
                myRoute.setBlock(routePos1, c2)

    @staticmethod
    def tournamentSelection(pop):

        popTournament = population(GA.tournamentSize, False)

        for i in range(GA.tournamentSize):
            randomID = random.randint(0, pop.populationSize() - 1)
            popTournament.saveRoute(i, pop.getRoute(randomID))

        fittestRoute = popTournament.getFittest()
        return fittestRoute


class Ambulance:

    def __init__(self, Start=0, End=0, isFree=True):
        self.start = RouteManager.getBlock(Start)
        self.end = RouteManager.getBlock(End)
        self.route = population(5, False)
        self.isFreeFlag = isFree

    def setStart(self, Start):
        self.start = RouteManager.getBlock(Start)

    def getStart(self):
        return self.start

    def setEnd(self, End):
        self.end = RouteManager.getBlock(End)

    def getEnd(self):
        return self.end

    def setAllRoutes(self, Population1):
        # self.route=population(1,False)
        self.route = Population1

    def getAllRoutes(self):
        return self.route

    def setRoute(self, index, newRoute):
        self.route.__setitem__(index, newRoute)

    def getRoute(self, index):
        return self.route[index]

    def setisFree(self, isFree):
        self.isFreeFlag = isFree

    def getisFree(self):
        return self.isFreeFlag

    def addRoute(self, route):
        self.route.append(route)
