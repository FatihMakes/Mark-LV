// Base destructible building: material-typed HP, support-graph collapse, and heat/save wiring.
#pragma once

#include "CoreMinimal.h"
#include "GameFramework/Actor.h"
#include "DestructibleBuildingActor.generated.h"

class UStaticMeshComponent;

UENUM(BlueprintType)
enum class EBuildingMaterial : uint8
{
	Glass,
	Concrete,
	Steel
};

USTRUCT(BlueprintType)
struct FSupportLink
{
	GENERATED_BODY()

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Destruction")
	TObjectPtr<AActor> LinkedActor = nullptr;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Destruction")
	bool bBroken = false;
};

DECLARE_DYNAMIC_MULTICAST_DELEGATE_OneParam(FOnBuildingCollapsed, AActor*, Building);

UCLASS()
class DESTRUCTION_API ADestructibleBuildingActor : public AActor
{
	GENERATED_BODY()

public:
	ADestructibleBuildingActor();

	UPROPERTY(BlueprintAssignable, Category = "Destruction")
	FOnBuildingCollapsed OnBuildingCollapsed;

	// Unique, stable id used to persist this building's destroyed state across saves/reloads.
	UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = "Destruction")
	FName BuildingId;

	UFUNCTION(BlueprintCallable, Category = "Destruction")
	void ApplyBuildingDamage(float RawDamage);

	UFUNCTION(BlueprintPure, Category = "Destruction")
	bool IsDestroyed() const { return bDestroyed; }

	UFUNCTION(BlueprintPure, Category = "Destruction")
	float GetHealthNormalized() const;

	// Called by the save subsystem on load to restore a previously-destroyed building without re-fracturing FX.
	UFUNCTION(BlueprintCallable, Category = "Destruction")
	void ForceDestroyedState();

	virtual float TakeDamage(float DamageAmount, struct FDamageEvent const& DamageEvent, class AController* EventInstigator, AActor* DamageCauser) override;

protected:
	virtual void BeginPlay() override;

	UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = "Destruction")
	EBuildingMaterial MaterialType = EBuildingMaterial::Concrete;

	UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = "Destruction")
	float MaxHealth = 3000.0f;

	UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "Destruction")
	float CurrentHealth = 3000.0f;

	UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = "Destruction")
	float CollapseThreshold = 0.5f; // Collapse once > 50% of supports are broken.

	UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = "Destruction")
	TArray<FSupportLink> SupportLinks;

	// Heat added to UCityHeatSubsystem when this building fractures/collapses.
	UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = "Destruction")
	int32 FractureHeatAmount = 1;

	UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = "Destruction")
	int32 CollapseHeatAmount = 2;

	UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "Destruction")
	bool bDestroyed = false;

	UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "Components")
	TObjectPtr<UStaticMeshComponent> BuildingMesh;

	UFUNCTION(BlueprintImplementableEvent, Category = "Destruction")
	void BP_OnFracture();

	UFUNCTION(BlueprintImplementableEvent, Category = "Destruction")
	void BP_OnCollapse();

private:
	float GetMaterialMultiplier() const;
	void EvaluateSupportCollapse();
	void CollapseBuilding();
	void NotifyHeat(int32 Amount);
};
